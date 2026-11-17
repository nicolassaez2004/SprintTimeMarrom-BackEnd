import pytest

from app import create_app


@pytest.fixture
def client():
    # Banco em memoria: os testes nao dependem do MySQL
    app = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
    })
    return app.test_client()


def test_exemplo_linha_valida(client):
    response = client.get("/api/exemplo?linha=8000")

    assert response.status_code == 200
    assert response.get_json() == {"linha": 8000}


@pytest.mark.parametrize("url", [
    "/api/exemplo",
    "/api/exemplo?linha=",
    "/api/exemplo?linha=abc",
    "/api/exemplo?linha=-5",
])
def test_exemplo_linha_invalida(client, url):
    response = client.get(url)

    assert response.status_code == 400
    assert "erro" in response.get_json()
