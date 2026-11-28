"""US02-T03: versão GTFS vigente por data de operação."""

from datetime import date, datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import event

from app import create_app, db
from app.gtfs.service import (
    VersaoGTFSAmbigua, VersaoGTFSInexistente, versao_vigente,
)
from app.models import (
    ArquivoArmazenado, ExecucaoColeta, ObservacaoTratada, RegistroBrutoGPS, VersaoGTFS,
)


@pytest.fixture
def app():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://"})
    with app.app_context():
        @event.listens_for(db.engine, "connect")
        def ativar_fks(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
        db.create_all()
        yield app
        db.session.remove()
        db.engine.dispose()


def versao(inicio, fim=None, capturada_em=None):
    digest = uuid4().hex * 2
    arquivo = ArquivoArmazenado(
        chave=f"gtfs/{digest}.zip", nome_original="feed.zip",
        mime_type="application/zip", tamanho_bytes=1, sha256=digest,
    )
    v = VersaoGTFS(
        hash=digest, vigencia_inicio=inicio, vigencia_fim=fim, arquivo=arquivo,
        capturada_em=capturada_em or datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    db.session.add_all([arquivo, v])
    db.session.commit()
    return v


def test_troca_de_versao_e_datas_limite(app):
    v1 = versao(date(2026, 1, 1), date(2026, 1, 31))
    v2 = versao(date(2026, 2, 1))  # vigência aberta

    assert versao_vigente(date(2026, 1, 1)).id == v1.id   # primeiro dia, inclusivo
    assert versao_vigente(date(2026, 1, 31)).id == v1.id  # último dia, inclusivo
    assert versao_vigente(date(2026, 2, 1)).id == v2.id   # troca no dia seguinte
    assert versao_vigente(date(2030, 6, 1)).id == v2.id   # fim NULL = aberta


def test_vigencia_nao_depende_da_data_de_captura(app):
    # Capturada depois do início publicado: vale a vigência, não o download.
    v = versao(date(2026, 3, 1), capturada_em=datetime(2026, 3, 20, tzinfo=timezone.utc))
    assert versao_vigente(date(2026, 3, 5)).id == v.id


def test_sem_versao_vigente(app):
    with pytest.raises(VersaoGTFSInexistente):
        versao_vigente(date(2026, 1, 1))  # base vazia
    versao(date(2026, 1, 10), date(2026, 1, 20))
    with pytest.raises(VersaoGTFSInexistente):
        versao_vigente(date(2026, 1, 9))   # antes do início
    with pytest.raises(VersaoGTFSInexistente):
        versao_vigente(date(2026, 1, 21))  # depois do fim


def test_vigencias_sobrepostas_nao_sao_escolhidas_em_silencio(app):
    v1 = versao(date(2026, 1, 1))  # aberta
    v2 = versao(date(2026, 1, 15), date(2026, 2, 15))
    assert versao_vigente(date(2026, 1, 14)).id == v1.id
    with pytest.raises(VersaoGTFSAmbigua) as erro:
        versao_vigente(date(2026, 1, 15))
    assert {v.id for v in erro.value.versoes} == {v1.id, v2.id}
    assert erro.value.data_operacao == date(2026, 1, 15)


def test_rejeita_instante_no_lugar_da_data(app):
    with pytest.raises(TypeError):
        versao_vigente(datetime(2026, 1, 1, 3, tzinfo=timezone.utc))


def test_nova_versao_nao_altera_observacoes_antigas(app):
    v1 = versao(date(2026, 1, 1), date(2026, 1, 31))
    ciclo = ExecucaoColeta(estado="OK", novos_persistidos=1)
    db.session.add(ciclo)
    db.session.flush()
    bruto = RegistroBrutoGPS(execucao_id=ciclo.id, prefixo_origem="5810",
                             coletado_em=datetime(2026, 1, 20, tzinfo=timezone.utc),
                             payload={"p": "5810"})
    db.session.add(bruto)
    db.session.flush()
    db.session.add(ObservacaoTratada(
        bruto_id=bruto.id, valida=True, versao_correspondencia="v1",
        versao_gtfs_id=versao_vigente(date(2026, 1, 20)).id,
    ))
    db.session.commit()

    versao(date(2026, 2, 1))  # atualização do GTFS
    db.session.expire_all()

    assert ObservacaoTratada.query.one().versao_gtfs_id == v1.id
    assert versao_vigente(date(2026, 1, 20)).id == v1.id
    assert db.session.get(VersaoGTFS, v1.id).vigencia_fim == date(2026, 1, 31)


# --- Revisão: feed sem feed_info.txt (caso real da SPTrans) ------------------------------

def _zip_sem_feed_info(conteudo):
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as arquivo:
        for nome in ("agency.txt", "stops.txt", "routes.txt", "trips.txt", "stop_times.txt", "calendar.txt"):
            arquivo.writestr(nome, f"header\n{conteudo}\n")
    return buffer.getvalue()


class _Cliente:
    def __init__(self, conteudo):
        self.conteudo = conteudo

    def baixar_para(self, url, destino):
        destino.write(self.conteudo)


def test_captura_nova_encerra_a_anterior_e_nao_fica_ambigua(app, tmp_path):
    from app.gtfs.service import capturar_gtfs

    v1 = capturar_gtfs(url="x", diretorio=tmp_path, cliente=_Cliente(_zip_sem_feed_info("v1")),
                       agora=datetime(2026, 11, 19, 14, tzinfo=timezone.utc))
    v2 = capturar_gtfs(url="x", diretorio=tmp_path, cliente=_Cliente(_zip_sem_feed_info("v2")),
                       agora=datetime(2026, 11, 24, 14, tzinfo=timezone.utc))
    db.session.expire_all()
    assert (db.session.get(VersaoGTFS, v1.id).vigencia_inicio, db.session.get(VersaoGTFS, v1.id).vigencia_fim) == (
        date(2026, 11, 19), date(2026, 11, 23))
    assert db.session.get(VersaoGTFS, v2.id).vigencia_fim is None
    assert versao_vigente(date(2026, 11, 23)).id == v1.id   # véspera da nova: ainda a antiga
    assert versao_vigente(date(2026, 11, 24)).id == v2.id   # antes: VersaoGTFSAmbigua
    assert versao_vigente(date(2027, 3, 1)).id == v2.id


def test_inicio_sem_feed_info_usa_o_dia_de_sao_paulo(app, tmp_path):
    from app.gtfs.service import capturar_gtfs

    # 23:30 de 27/11 em São Paulo = 02:30 UTC de 28/11.
    v = capturar_gtfs(url="x", diretorio=tmp_path, cliente=_Cliente(_zip_sem_feed_info("v")),
                      agora=datetime(2026, 11, 28, 2, 30, tzinfo=timezone.utc))
    assert v.vigencia_inicio == date(2026, 11, 27)


def test_vigencia_explicita_nunca_e_alterada(app, tmp_path):
    from app.gtfs.service import encerrar_vigencias_abertas

    explicita = versao(date(2026, 1, 1), date(2026, 6, 30))
    aberta = versao(date(2026, 2, 1), capturada_em=datetime(2026, 2, 1, tzinfo=timezone.utc))
    nova = versao(date(2026, 3, 1), capturada_em=datetime(2026, 3, 1, tzinfo=timezone.utc))
    assert encerrar_vigencias_abertas(nova) == [aberta]
    db.session.commit()
    assert db.session.get(VersaoGTFS, explicita.id).vigencia_fim == date(2026, 6, 30)
    assert db.session.get(VersaoGTFS, aberta.id).vigencia_fim == date(2026, 2, 28)


def test_correcao_de_bases_antigas_encerra_em_ordem_de_captura(app):
    from app.gtfs.service import corrigir_vigencias_abertas

    a = versao(date(2026, 11, 19), capturada_em=datetime(2026, 11, 19, 14, tzinfo=timezone.utc))
    b = versao(date(2026, 11, 24), capturada_em=datetime(2026, 11, 24, 14, tzinfo=timezone.utc))
    c = versao(date(2026, 11, 26), capturada_em=datetime(2026, 11, 26, 14, tzinfo=timezone.utc))
    with pytest.raises(VersaoGTFSAmbigua):
        versao_vigente(date(2026, 11, 26))
    assert corrigir_vigencias_abertas() == 2
    db.session.commit()
    assert [versao_vigente(date(2026, 11, d)).id for d in (19, 23, 24, 25, 26, 30)] == [a.id, a.id, b.id, b.id, c.id, c.id]
    assert corrigir_vigencias_abertas() == 0  # idempotente
