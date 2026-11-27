from datetime import datetime, timedelta, timezone

import pytest
from app import create_app, db
from app.models import ExecucaoColeta
from app.monitoramento.sucesso_coleta import classificar_ciclo, serie_sucesso_coleta


@pytest.fixture
def app():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://"})
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.engine.dispose()


@pytest.mark.parametrize("erro,categoria", [
    ("SPTRANS_HTTP_503", "falha_fonte"), ("SPTransHTTPError: 503", "falha_fonte"),
    ("SPTransTimeoutError", "falha_fonte"), ("SPTRANS_HTTP_401", "falha_propria"),
    ("SPTransConfiguracaoError", "falha_propria"), ("SPTransAutenticacaoError", "falha_propria"),
    ("OperationalError", "falha_propria"), ("SPTRANS_DESCONHECIDA", "desconhecida"),
    (None, "desconhecida"), ("ErroSemClassificacao", "desconhecida"),
])
def test_classificacao_sem_confundir_fonte_e_plataforma(erro, categoria):
    assert classificar_ciclo(ExecucaoColeta(estado="FALHA", erro=erro)) == categoria


def test_serie_reconstroi_ciclos_e_exclui_fonte(app):
    inicio = datetime(2026, 11, 27, 12, tzinfo=timezone.utc)
    estados = [("OK", None), ("OK", None), ("FALHA", "OperationalError"),
               ("FALHA", "SPTRANS_HTTP_503"), ("PARCIAL", None),
               ("EM_ANDAMENTO", None), ("FALHA", None)]
    for i, (estado, erro) in enumerate(estados):
        db.session.add(ExecucaoColeta(fonte="OLHO_VIVO", prevista_em=inicio + timedelta(minutes=i),
            iniciada_em=inicio + timedelta(minutes=i), estado=estado, erro=erro))
    db.session.add(ExecucaoColeta(fonte="OUTRA", iniciada_em=inicio, estado="OK"))
    db.session.commit()
    resposta = serie_sucesso_coleta(inicio, inicio + timedelta(hours=2))
    primeiro, vazio = resposta["pontos"]
    assert primeiro["contagens"] == {"sucesso": 2, "falha_propria": 1, "falha_fonte": 1,
                                     "parcial": 1, "em_andamento": 1, "desconhecida": 1}
    assert primeiro["total_registrado"] == 7
    assert primeiro["denominador_sem_falhas_fonte"] == 3
    assert primeiro["percentual_sucesso"] == 66.67
    assert {x["id"] for x in primeiro["ciclos"]} == {e.id for e in ExecucaoColeta.query.filter_by(fonte="OLHO_VIVO")}
    assert vazio["denominador_sem_falhas_fonte"] == 0 and vazio["percentual_sucesso"] is None


def test_fronteiras_e_parametros(app):
    inicio = datetime(2026, 11, 27, 12, tzinfo=timezone.utc)
    db.session.add(ExecucaoColeta(iniciada_em=inicio + timedelta(hours=1), estado="OK"))
    db.session.commit()
    pontos = serie_sucesso_coleta(inicio, inicio + timedelta(hours=2))["pontos"]
    assert [p["total_registrado"] for p in pontos] == [0, 1]
    with pytest.raises(ValueError):
        serie_sucesso_coleta(inicio.replace(tzinfo=None), inicio)
    with pytest.raises(ValueError):
        serie_sucesso_coleta(inicio, inicio + timedelta(days=32))
