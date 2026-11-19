"""Transações da coleta; escrita protegida por token e chave lógica do ciclo."""

from uuid import uuid4

from app import db
from app.models import (
    EstadoColeta,
    ExecucaoColeta,
    LacunaColeta,
    NotificacaoColeta,
    RegistroBrutoGPS,
)
from app.monitoramento.journal import instante


class PossePerdida(RuntimeError):
    pass


def listar_alertas(limite=100):
    """Caixa compartilhada da equipe, independentemente dos arquivos do PC."""
    linhas = (
        db.session.query(NotificacaoColeta, LacunaColeta)
        .outerjoin(LacunaColeta, NotificacaoColeta.lacuna_id == LacunaColeta.id)
        .order_by(NotificacaoColeta.criada_em.desc(), NotificacaoColeta.id)
        .limit(limite)
        .all()
    )
    mensagens = {
        "TESTE": "TESTE US29: caixa interna funcionando. Não representa falha real da coleta.",
        "FALHA": "Falha contínua da coleta há pelo menos 10 minutos.",
        "RECUPERACAO": "Coleta recuperada e gravação confirmada.",
    }
    return [
        dict(
            id=n.id,
            lacuna_id=n.lacuna_id,
            tipo=n.tipo,
            simulado=n.tipo == "TESTE",
            criada_em=n.criada_em.isoformat(),
            enviada_em=n.enviada_em.isoformat() if n.enviada_em else None,
            canal=n.canal,
            inicio=l.inicio.isoformat() if l else None,
            fim=l.fim.isoformat() if l and l.fim else None,
            causa=l.causa if l else None,
            mensagem=mensagens.get(n.tipo, n.tipo),
        )
        for n, l in linhas
    ]


class ColetaRepository:
    def __init__(self):
        self.token = str(uuid4())

    def assumir(self):
        estado = (
            db.session.query(EstadoColeta)
            .filter_by(fonte="OLHO_VIVO")
            .with_for_update()
            .one_or_none()
        )
        if estado is None:
            estado = EstadoColeta(fonte="OLHO_VIVO", token=self.token)
            db.session.add(estado)
        else:
            estado.token = self.token
        db.session.commit()

    def reservar(self):
        estado = (
            db.session.query(EstadoColeta)
            .filter_by(fonte="OLHO_VIVO")
            .with_for_update()
            .one()
        )
        if estado.token != self.token:
            raise PossePerdida("Outro worker assumiu a coleta")

    def _upsert(self, tabela, registros, imutaveis):
        if not registros:
            return
        if db.engine.dialect.name == "mysql":
            from sqlalchemy.dialects.mysql import insert

            s = insert(tabela).values(registros)
            s = s.on_duplicate_key_update(
                **{
                    c.name: s.inserted[c.name]
                    for c in tabela.columns
                    if c.name not in imutaveis
                }
            )
        else:
            from sqlalchemy.dialects.sqlite import insert

            s = insert(tabela).values(registros)
            s = s.on_conflict_do_update(
                index_elements=["id"],
                set_={
                    c.name: s.excluded[c.name]
                    for c in tabela.columns
                    if c.name not in imutaveis
                },
            )
        db.session.execute(s)

    def sincronizar(self, snapshot):
        lacunas = [
            dict(
                id=l["id"],
                inicio=instante(l["inicio"]),
                fim=instante(l["fim"]) if l["fim"] else None,
                causa=l["causa"],
                fonte=l["fonte"],
                aberta_fonte=l["fonte"] if l["fim"] is None else None,
            )
            for l in snapshot["lacunas"]
        ]
        self._upsert(
            LacunaColeta.__table__, lacunas, {"id", "inicio", "causa", "fonte"}
        )
        alertas = [
            dict(
                id=a["id"],
                lacuna_id=a["lacuna_id"],
                tipo=a["tipo"],
                criada_em=instante(a["criada_em"]),
                enviada_em=instante(a["enviada_em"]),
                canal=a["canal"],
            )
            for a in snapshot["alertas"]
        ]
        self._upsert(
            NotificacaoColeta.__table__,
            alertas,
            {"id", "lacuna_id", "tipo", "criada_em"},
        )

    def inicio(self, ciclo, snapshot):
        self.reservar()
        self.sincronizar(snapshot)
        e = (
            ExecucaoColeta.query.filter_by(chave_logica=ciclo["chave"])
            .with_for_update()
            .one_or_none()
        )
        if e is None:
            e = ExecucaoColeta(
                chave_logica=ciclo["chave"],
                fonte="OLHO_VIVO",
                iniciada_em=instante(ciclo["inicio"]),
                prevista_em=instante(ciclo["prevista_em"])
                if ciclo["prevista_em"]
                else None,
                estado="EM_ANDAMENTO",
                tentativas=0,
                recebidos=0,
                novos_persistidos=0,
            )
            db.session.add(e)
        db.session.commit()

    def concluir(self, ciclo, snapshot, agora):
        from app.coleta.bus_collector import BusCollector

        self.reservar()
        self.sincronizar(snapshot)
        e = (
            ExecucaoColeta.query.filter_by(chave_logica=ciclo["chave"])
            .with_for_update()
            .one()
        )
        if e.estado == "OK":
            # Commit anterior com resposta perdida: a própria execução confirma
            # que o lote inteiro já existe, inclusive GPS com chave incompleta.
            total = e.novos_persistidos
            db.session.commit()
            return total
        e.tentativas = ciclo["tentativas"]
        e.erro = ciclo["erro"]
        e.lacuna_id = ciclo["lacuna_id"]
        e.finalizada_em = agora
        if ciclo["final_falha"]:
            e.estado = "FALHA"
            e.recebidos = 0
            e.novos_persistidos = 0
        else:
            brutos = BusCollector._ler_frota(
                ciclo["resposta"], e.id, instante(ciclo["coletado_em"])
            )
            novos = BusCollector._descartar_duplicados(brutos)
            BusCollector._registrar_veiculos_novos(novos)
            db.session.flush()
            registros = [
                {
                    c.name: getattr(r, c.name)
                    for c in RegistroBrutoGPS.__table__.columns
                    if c.name != "id"
                }
                for r in novos
            ]
            for i in range(0, len(registros), 250):
                db.session.execute(
                    RegistroBrutoGPS.__table__.insert().values(registros[i : i + 250])
                )
            e.estado = "OK"
            e.erro = None
            e.recebidos = len(brutos)
            e.novos_persistidos = len(novos)
        total = e.novos_persistidos
        db.session.commit()
        return total

    def confirmar(self, ciclo, snapshot):
        self.reservar()
        self.sincronizar(snapshot)
        e = (
            ExecucaoColeta.query.filter_by(chave_logica=ciclo["chave"])
            .with_for_update()
            .one()
        )
        if e.estado == "OK" and e.confirmada_em is None:
            e.confirmada_em = instante(ciclo["confirmada_em"])
            e.finalizada_em = e.confirmada_em
            e.duracao_gravacao_ms = ciclo["duracao_ms"]
        db.session.commit()

    def restaurar(self, journal, intervalo, agora):
        from datetime import timedelta

        self.reservar()
        ativa = LacunaColeta.query.filter_by(aberta_fonte="OLHO_VIVO").one_or_none()
        if ativa and not any(l["id"] == ativa.id for l in journal.dados["lacunas"]):
            journal.importar_lacuna(
                dict(
                    id=ativa.id,
                    inicio=ativa.inicio.isoformat(),
                    fim=None,
                    causa=ativa.causa,
                    fonte=ativa.fonte,
                )
            )
        pendente = journal.dados["ciclo"]
        chave = (pendente or {}).get("chave")
        if pendente and pendente["confirmada_em"] is None:
            id = journal.abrir_lacuna(
                "PROCESSO_INTERROMPIDO", instante(pendente["inicio"])
            )
            journal.atualizar_ciclo(lacuna_id=id)
        interrompidas = ExecucaoColeta.query.filter_by(
            estado="EM_ANDAMENTO", fonte="OLHO_VIVO"
        ).all()
        for e in interrompidas:
            if chave is not None and e.chave_logica == chave:
                continue
            id = journal.abrir_lacuna("PROCESSO_INTERROMPIDO", e.iniciada_em)
            self.sincronizar(journal.snapshot())
            e.estado = "FALHA"
            e.erro = "PROCESSO_INTERROMPIDO"
            e.finalizada_em = agora
            e.lacuna_id = id
        ultima = (
            ExecucaoColeta.query.filter_by(fonte="OLHO_VIVO", estado="OK")
            .filter(ExecucaoColeta.prevista_em.is_not(None))
            .order_by(ExecucaoColeta.iniciada_em.desc())
            .first()
        )
        if ultima:
            proxima = ultima.prevista_em + timedelta(seconds=intervalo)
            if agora > proxima and chave is None:
                journal.abrir_lacuna("PROCESSO_INTERROMPIDO", proxima)
        self.sincronizar(journal.snapshot())
        db.session.commit()
        journal.salvar()
