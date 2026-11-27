from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import OperationalError

from app import create_app, db
from app.models import ExecucaoColeta, LacunaColeta, VerificacaoDisponibilidade


@pytest.fixture
def app(monkeypatch):
    import app.api.saude as rota
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://"})
    agora = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(rota, "agora_utc", lambda: agora)
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.engine.dispose()


def test_api_indisponivel_coleta_com_falha_e_mes_parcial(app):
    agora = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
    instante = agora - timedelta(minutes=3)
    db.session.add_all([
        VerificacaoDisponibilidade(alvo="API", instante=instante, duracao_ms=50, sucesso=False,
                                    status_http=503, resultado="HTTP_ERRO"),
        VerificacaoDisponibilidade(alvo="PAINEL", instante=instante, duracao_ms=30, sucesso=True,
                                    status_http=200, resultado="OK"),
        ExecucaoColeta(fonte="OLHO_VIVO", iniciada_em=instante, estado="FALHA", erro="SPTRANS_HTTP_503"),
        LacunaColeta(id="incidente-1", inicio=instante, fim=None, fonte="OLHO_VIVO", causa="SPTRANS_HTTP_503"),
    ])
    db.session.commit()
    resposta = app.test_client().get("/saude?mes=2026-11")
    assert resposta.status_code == 200
    dados = resposta.get_json()
    assert dados["mes_parcial"] is True
    assert dados["servicos"]["API"]["ultima_medicao"]["sucesso"] is False
    assert dados["servicos"]["API"]["ultima_medicao"]["status_http"] == 503
    assert dados["servicos"]["PAINEL"]["ultima_medicao"]["sucesso"] is True
    assert dados["servicos"]["API"]["meta_mensal_comprovada"] is None
    assert dados["servicos"]["API"]["minutos_sem_sondagem"] > 0
    assert dados["coleta"]["contagens"]["falha_fonte"] == 1
    assert dados["incidentes_coleta"]["total"] == 1
    assert dados["incidentes_coleta"]["itens"][0]["estado_no_corte"] == "ABERTA"
    assert dados["limites"]


def test_validacao_ausencia_e_aliased_api(app):
    client = app.test_client()
    dados = client.get("/api/saude").get_json()
    assert dados["mes"] == "2026-11" and dados["servicos"]["API"]["ultima_medicao"] is None
    for caminho in ("/saude?mes=2026-12", "/saude?mes=9999-12", "/saude?mes=x", "/saude?mes=2026-11&mes=2026-10", "/saude?x=1"):
        resposta = client.get(caminho)
        assert resposta.status_code == 400
        assert resposta.get_json()["erro"]["codigo"] == "PARAMETRO_INVALIDO"


def test_erro_de_banco_nao_expoe_detalhes(app, monkeypatch):
    import app.api.saude as rota
    def falhar(*args, **kwargs):
        raise OperationalError("SQL secreto", {}, Exception("credencial"))
    monkeypatch.setattr(rota, "consultar_saude", falhar)
    resposta = app.test_client().get("/saude")
    assert resposta.status_code == 503
    assert resposta.get_json()["erro"]["codigo"] == "CONSULTA_INDISPONIVEL"
    assert "secreto" not in resposta.get_data(as_text=True)
