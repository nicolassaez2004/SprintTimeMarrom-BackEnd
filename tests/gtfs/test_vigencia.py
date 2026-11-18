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
