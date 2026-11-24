import json
from pathlib import Path

import pytest

from app import create_app, db
from app.models import IndicadorOperacional, LimiteConsultaIP
from scripts.medir_api_indicadores import percentil


@pytest.fixture
def cliente():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://", "MYSQL_SSL_CA": None,
                      "INDICADORES_TRUSTED_PROXY_CIDRS": "", "LIMITES_RELOGIO": lambda: 120})
    with app.app_context():
        db.metadata.create_all(db.engine, tables=[IndicadorOperacional.__table__, LimiteConsultaIP.__table__])
    yield app.test_client()
    with app.app_context():
        db.session.remove()
        db.engine.dispose()


def test_guia_contrato_e_evidencia_publicos_nao_consumem_cota(cliente):
    for _ in range(60):
        assert cliente.get("/indicadores").status_code == 200
    assert cliente.get("/series").status_code == 429
    guia = cliente.get("/docs/indicadores")
    assert guia.status_code == 200 and guia.mimetype == "text/html"
    assert "Retry-After" in guia.text and "p95" in guia.text and "sintéticos" in guia.text
    assert "<script" not in guia.text  # Exemplos são texto, não consomem a API ao abrir.
    contrato = cliente.get("/docs/indicadores/openapi.yaml")
    arquivo = Path(__file__).resolve().parents[2] / "docs/openapi.yaml"
    assert contrato.status_code == 200 and contrato.data == arquivo.read_bytes()
    ensaio = cliente.get("/docs/indicadores/ensaio.json")
    assert ensaio.status_code == 200 and ensaio.mimetype == "application/json"
    assert ensaio.get_json()["dados"]["origem"] == "SINTETICA"
    assert "X-RateLimit-Remaining" not in guia.headers
    assert cliente.get("/indicadores").status_code == 429
    # Mantém o comportamento antigo: não foi instalado gerador automático de Swagger.
    assert cliente.get("/docs").status_code == 404
    assert cliente.get("/openapi.yaml").status_code == 404


def test_percentil_e_evidencia_preservam_todas_as_amostras_sem_ocultar_falhas():
    assert percentil(list(range(1, 101)), 95) == 95
    with pytest.raises(ValueError):
        percentil([], 95)
    caminho = Path(__file__).resolve().parents[2] / "docs/evidencias/US11-T03-desempenho.json"
    registro = json.loads(caminho.read_text(encoding="utf-8"))
    medicoes = registro["medicoes"]
    assert len(medicoes) == registro["metodo"]["amostras"] == 100
    assert registro["metodo"]["falhas_excluidas"] == 0
    assert all(m["status"] == 200 for m in medicoes)
    assert percentil([m["ms"] for m in medicoes], 95) == registro["resumo"]["p95_ms"]
    assert registro["resumo"]["p95_ms"] <= 2000
    assert registro["limite_http"]["61_status"] == 429
    assert registro["limite_http"]["nova_janela_status"] == 200
    assert registro["aceite"] == "PENDENTE_REVISAO"


def test_contrato_openapi_e_yaml_valido():
    yaml = pytest.importorskip("yaml")  # PyYAML não é dependência do produto

    arquivo = Path(__file__).resolve().parents[2] / "docs/openapi.yaml"
    contrato = yaml.safe_load(arquivo.read_text(encoding="utf-8"))
    assert {"Indicador", "LadoEspera", "PaginaSeries"} <= set(contrato["components"]["schemas"])
