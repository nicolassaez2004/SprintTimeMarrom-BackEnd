import pytest

from datetime import datetime, timezone

from app import create_app, db
from app.models import IndicadorOperacional


@pytest.fixture
def client():
    app = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
    })
    with app.app_context():
        db.metadata.create_all(bind=db.engine, tables=[IndicadorOperacional.__table__])
    return app.test_client()


@pytest.mark.parametrize("path", ["/indicadores", "/series"])
def test_rotas_publicas_sem_resultados_retornam_pagina_vazia(client, path):
    response = client.get(path)

    assert response.status_code == 200
    assert response.get_json() == {"dados": [], "paginacao": {"page": 1, "page_size": 100, "total": 0}}


@pytest.mark.parametrize("path,series", [("/indicadores", False), ("/series", True)])
def test_rotas_consultam_resultados_publicados_com_paginacao(client, path, series):
    inicio = datetime(2026, 11, 23, tzinfo=timezone.utc)
    fim = datetime(2026, 11, 24, tzinfo=timezone.utc)
    with client.application.app_context():
        db.session.add_all([
            IndicadorOperacional(
                id=f"teste-{n}", linha_codigo="8000", indicador="pontualidade",
                valor=87.5 + n, unidade="%",
                periodo_inicio=inicio.replace(hour=n), periodo_fim=fim,
                versao_conjunto="2026-11-v1",
                qualidade={"cobertura": 0.98, "observacoes": 1250},
            ) for n in range(2)
        ])
        db.session.commit()

    response = client.get(path + "?linha=8000&page=2&page_size=1")
    body = response.get_json()
    assert response.status_code == 200
    assert body["paginacao"] == {"page": 2, "page_size": 1, "total": 2}
    assert len(body["dados"]) == 1
    assert body["dados"][0]["unidade"] == "%"
    assert body["dados"][0]["versao_conjunto"] == "2026-11-v1"
    assert body["dados"][0]["instante" if series else "inicio"].startswith("2026-11-23")


@pytest.mark.parametrize("path", ["/indicadores", "/series"])
@pytest.mark.parametrize("query", [
    "?page=0", "?page_size=101", "?page=1&page=2", "?desconhecido=x",
    "?inicio=2026-11-23", "?inicio=2026-11-24T00:00:00Z&fim=2026-11-23T00:00:00Z",
    "?linha=abc", "?indicador=bad%20name",
])
def test_parametros_invalidos_sao_rejeitados_com_400(client, path, query):
    response = client.get(path + query)

    assert response.status_code == 400
    erro = response.get_json()["erro"]
    assert erro["codigo"] == "PARAMETRO_INVALIDO"
    assert erro["campo"]


def test_openapi_nao_e_publicado_como_interface_automatica(client):
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.yaml").status_code == 404
