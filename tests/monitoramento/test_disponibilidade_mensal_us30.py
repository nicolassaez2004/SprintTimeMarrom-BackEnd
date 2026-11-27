from datetime import datetime, timedelta, timezone

import pytest
from app import create_app, db
from app.models import ExecucaoColeta, VerificacaoDisponibilidade
from app.monitoramento.disponibilidade_mensal import disponibilidade_mensal


@pytest.fixture
def app():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://"})
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.engine.dispose()


def medicao(alvo, instante, sucesso):
    return VerificacaoDisponibilidade(alvo=alvo, instante=instante, duracao_ms=10,
        sucesso=sucesso, status_http=200 if sucesso else 503, resultado="OK" if sucesso else "HTTP_ERRO")


def test_lacunas_duplicatas_e_fonte_sao_independentes(app):
    inicio = datetime(2026, 11, 1, 3, tzinfo=timezone.utc)
    db.session.add_all([medicao("API", inicio, True), medicao("API", inicio + timedelta(seconds=30), False),
        medicao("API", inicio + timedelta(minutes=1), True), medicao("PAINEL", inicio, True),
        ExecucaoColeta(fonte="OLHO_VIVO", iniciada_em=inicio, estado="FALHA", erro="SPTRANS_HTTP_503"),
        ExecucaoColeta(fonte="OLHO_VIVO", iniciada_em=inicio, estado="FALHA", erro="OperationalError")])
    db.session.commit()
    relatorio = disponibilidade_mensal("2026-11", agora=inicio + timedelta(minutes=3, seconds=45))
    api, painel = relatorio["servicos"]["API"], relatorio["servicos"]["PAINEL"]
    assert relatorio["mes_parcial"]
    assert (api["minutos_esperados"], api["minutos_observados"], api["minutos_sem_sondagem"]) == (3, 2, 1)
    assert (api["minutos_sucesso"], api["minutos_falha"], api["registros_adicionais_no_mesmo_minuto"]) == (1, 1, 1)
    assert api["percentual_observado"] == 50 and api["meta_mensal_comprovada"] is None
    assert painel["percentual_observado"] == 100 and painel["minutos_sem_sondagem"] == 2
    assert relatorio["coleta"]["contagens"]["falha_fonte"] == 1
    assert relatorio["coleta"]["contagens"]["falha_propria"] == 1


def test_mes_encerrado_sem_medicao_nao_afirma_meta(app):
    resultado = disponibilidade_mensal("2026-02", agora=datetime(2026, 3, 1, 3, tzinfo=timezone.utc))
    api = resultado["servicos"]["API"]
    assert not resultado["mes_parcial"]
    assert api["minutos_esperados"] == 28 * 24 * 60
    assert api["denominador"] == 0 and api["percentual_observado"] is None
    assert api["meta_mensal_comprovada"] is None


def test_parametros_invalidos(app):
    agora = datetime(2026, 11, 1, 3, tzinfo=timezone.utc)
    for mes in ("2026-13", "2026-2", "2026-12"):
        with pytest.raises(ValueError):
            disponibilidade_mensal(mes, agora=agora)
    with pytest.raises(ValueError):
        disponibilidade_mensal("2026-11", agora=agora.replace(tzinfo=None))


def test_meta_99_so_com_cobertura_completa(app, monkeypatch):
    from app.monitoramento import disponibilidade_mensal as modulo
    inicio = datetime(2026, 11, 1, 3, tzinfo=timezone.utc)
    fim = inicio + timedelta(minutes=100)
    monkeypatch.setattr(modulo, "_mes", lambda _: (inicio, fim))
    db.session.add_all([medicao("API", inicio + timedelta(minutes=i), i != 0) for i in range(100)])
    db.session.commit()
    resultado = modulo.disponibilidade_mensal("2026-11", agora=fim)
    api = resultado["servicos"]["API"]
    assert api["percentual_observado"] == 99 and api["meta_mensal_comprovada"] is True
    assert resultado["servicos"]["PAINEL"]["meta_mensal_comprovada"] is None
