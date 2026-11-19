"""Persistência compartilhada pelo MatchingService e pelo relatório de qualidade."""
from sqlalchemy import func
from types import SimpleNamespace
from uuid import uuid4
from app import db
from app.models import (TabelaCorrespondencia, CorrespondenciaCodigo, Trecho,
                        RegistroBrutoGPS, ObservacaoTratada)


class CorrespondenciaRepository:
    def tabelas(self, versao_id, dia):
        tabelas = TabelaCorrespondencia.query.filter(
            TabelaCorrespondencia.versao_gtfs_id == versao_id,
            TabelaCorrespondencia.vigencia_inicio <= dia,
            db.or_(TabelaCorrespondencia.vigencia_fim.is_(None),
                   TabelaCorrespondencia.vigencia_fim >= dia)).order_by(TabelaCorrespondencia.versao).all()
        substituidas = {t.substitui_id for t in tabelas if t.substitui_id}
        return [t for t in tabelas if t.id not in substituidas]

    def candidatos(self, tabelas, codigo, sentido):
        if not tabelas or codigo is None or sentido is None:return []
        return CorrespondenciaCodigo.query.filter(
            CorrespondenciaCodigo.tabela_id.in_([t.id for t in tabelas]),
            CorrespondenciaCodigo.codigo_olho_vivo == codigo,
            CorrespondenciaCodigo.sentido_olho_vivo == sentido,
        ).order_by(CorrespondenciaCodigo.id).all()

    def trechos(self, versao_id):
        return Trecho.query.filter_by(versao_gtfs_id=versao_id).all()

    def bruto_reservado(self, id):
        # Serializa retentativas do mesmo bruto no MySQL. Não escreve na origem.
        return RegistroBrutoGPS.query.filter_by(id=id).with_for_update().one()

    def observacao(self, bruto_id):
        # Leitura corrente após reservar o bruto: evita snapshot antigo no
        # REPEATABLE READ quando outro worker confirmou enquanto aguardávamos.
        return ObservacaoTratada.query.filter_by(bruto_id=bruto_id).with_for_update().one_or_none()

    def guardar(self, obs):
        db.session.add(obs)
        db.session.flush()

    def grupos_diarios(self, inicio, fim):
        return db.session.query(
            ObservacaoTratada.valida, ObservacaoTratada.linha_codigo,
            ObservacaoTratada.versao_gtfs_id, ObservacaoTratada.versao_correspondencia,
            ObservacaoTratada.versao_validacao, ObservacaoTratada.motivo_correspondencia,
            ObservacaoTratada.motivo_invalidez, ObservacaoTratada.processada_em.is_not(None),
            func.count(RegistroBrutoGPS.id),
        ).select_from(RegistroBrutoGPS).outerjoin(ObservacaoTratada,
            ObservacaoTratada.bruto_id==RegistroBrutoGPS.id).filter(
            RegistroBrutoGPS.coletado_em>=inicio, RegistroBrutoGPS.coletado_em<fim).group_by(
            ObservacaoTratada.valida, ObservacaoTratada.linha_codigo,
            ObservacaoTratada.versao_gtfs_id, ObservacaoTratada.versao_correspondencia,
            ObservacaoTratada.versao_validacao, ObservacaoTratada.motivo_correspondencia,
            ObservacaoTratada.motivo_invalidez, ObservacaoTratada.processada_em.is_not(None),
        ).all()

    def amostra(self, inicio, fim, limite):
        return db.session.query(RegistroBrutoGPS, ObservacaoTratada).join(
            ObservacaoTratada, ObservacaoTratada.bruto_id==RegistroBrutoGPS.id).filter(
            RegistroBrutoGPS.coletado_em>=inicio, RegistroBrutoGPS.coletado_em<fim,
            ObservacaoTratada.processada_em.is_not(None), ObservacaoTratada.valida.is_(True),
            ObservacaoTratada.linha_codigo.is_(None),
        ).order_by(RegistroBrutoGPS.id).limit(limite).all()


class CorrespondenciaLoteRepository(CorrespondenciaRepository):
    """Reserva o lote de uma vez e confirma os tratados na transação do job."""

    def __init__(self):
        self.brutos = {}
        self.observacoes = {}
        self.vinculos = {}
        self.tabelas_vigentes = {}
        self.novas = {}

    def preparar(self, ids):
        if self.novas:
            raise ValueError('Confirme as novas observações antes de preparar outro lote')
        self.brutos = {r.id: r for r in RegistroBrutoGPS.query.filter(
            RegistroBrutoGPS.id.in_(ids)).order_by(RegistroBrutoGPS.id).with_for_update().all()}
        if len(self.brutos) != len(ids):
            raise ValueError('Lote contém bruto inexistente ou ID repetido')
        self.observacoes = {r.bruto_id: r for r in ObservacaoTratada.query.filter(
            ObservacaoTratada.bruto_id.in_(ids)).with_for_update().all()}

    def bruto_reservado(self, id):
        return self.brutos[id]

    def observacao(self, bruto_id):
        return self.observacoes.get(bruto_id)

    def tabelas(self, versao_id, dia):
        chave = (versao_id, dia)
        if chave not in self.tabelas_vigentes:
            self.tabelas_vigentes[chave] = [SimpleNamespace(id=t.id, versao=t.versao)
                for t in super().tabelas(versao_id, dia)]
        return self.tabelas_vigentes[chave]

    def candidatos(self, tabelas, codigo, sentido):
        chave = tuple(sorted(t.id for t in tabelas))
        if not chave or codigo is None or sentido is None:
            return []
        if chave not in self.vinculos:
            grupos = {}
            for c in CorrespondenciaCodigo.query.filter(
                    CorrespondenciaCodigo.tabela_id.in_(chave)).order_by(CorrespondenciaCodigo.id):
                # Valores do snapshot do job não expiram a cada commit de lote.
                valor = SimpleNamespace(**{k: getattr(c, k) for k in (
                    'id', 'tabela_id', 'codigo_olho_vivo', 'sentido_olho_vivo',
                    'linha_codigo', 'sentido_gtfs', 'motivo')})
                grupos.setdefault((c.codigo_olho_vivo, c.sentido_olho_vivo), []).append(valor)
            self.vinculos[chave] = grupos
        return self.vinculos[chave].get((codigo, sentido), [])

    def guardar(self, obs):
        obs.id = obs.id or str(uuid4())
        if obs in db.session:
            db.session.expunge(obs)
        self.novas[obs.bruto_id] = obs
        self.observacoes[obs.bruto_id] = obs

    def confirmar(self):
        registros = [{c.name: getattr(obs, c.name) for c in ObservacaoTratada.__table__.columns}
                     for obs in self.novas.values()]
        for inicio in range(0, len(registros), 250):
            tabela = ObservacaoTratada.__table__
            lote = registros[inicio:inicio + 250]
            campos = [c.name for c in tabela.columns if c.name not in ('id', 'bruto_id')]
            if db.engine.dialect.name == 'mysql':
                from sqlalchemy.dialects.mysql import insert
                stmt = insert(tabela).values(lote)
                stmt = stmt.on_duplicate_key_update(**{c: stmt.inserted[c] for c in campos})
            else:
                from sqlalchemy.dialects.sqlite import insert
                stmt = insert(tabela).values(lote)
                stmt = stmt.on_conflict_do_update(index_elements=['bruto_id'],
                    set_={c: stmt.excluded[c] for c in campos})
            db.session.execute(stmt)
        self.novas.clear()
