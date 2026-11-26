"""US08-T03: GET /contexto/trafego lê a velocidade por trecho/janela gravada pela US08-T02.

Dados sintéticos (teste controlado), não validação com dados reais da SPTrans.
"""

import socket
from datetime import timedelta

import pytest

from app import db
from app.contexto.trafego import FONTE_AGREGADO, Limites, calcular_viagem
from app.correspondencia.geografia import PROJECAO
from app.consultas.trafego import consultar_trafego_trecho, janelas_inteiras
from app.models import MedicaoContexto, Trecho
from tests.indicadores.test_velocidade_15min_us08_t02 import (  # noqa: F401  (fixture)
    LACUNA, LAT, PARADAS, T0, VERSAO_EST, agregar_banco, cenario, viagem,
)

FIM = T0 + timedelta(minutes=45)


def iso(instante):
    return instante.isoformat().replace("+00:00", "Z")


def get(app, trecho_id, inicio=T0, fim=FIM, **extra):
    consulta = {"trecho_id": trecho_id, "inicio": iso(inicio), "fim": iso(fim), **extra}
    return app.test_client().get("/contexto/trafego", query_string=consulta)


def trechos():
    return Trecho.query.order_by(Trecho.ordem).all()


@pytest.fixture
def sem_rede(monkeypatch):
    """Qualquer tentativa de rede (Waze ou outra) falha o teste."""
    def bloquear(*_, **__):
        raise AssertionError("o caminho padrão não pode acessar a rede")
    monkeypatch.setattr(socket, "create_connection", bloquear)
    monkeypatch.setattr(socket.socket, "connect", bloquear)


def test_conferencia_amostra_agregado_registro_e_resposta(app, cenario, sem_rede):
    """A→B ≈ 306,30 m em 30 s: T01 → T02 → MedicaoContexto → endpoint com o mesmo valor."""
    v = viagem(cenario, 0, "5810", (0, "A"), (30, "B"))
    (x1, x2), (y1, y2) = PROJECAO.transform([PARADAS["A"], PARADAS["B"]], [LAT, LAT])
    esperado = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5 / 30 * 3.6
    amostra = calcular_viagem(v, VERSAO_EST, Limites(LACUNA, 1.0, 80.0))[0][0]   # 1. T01
    assert amostra.velocidade_kmh == pytest.approx(esperado, rel=1e-9)
    janela = next(j for j in agregar_banco(cenario) if j.motivo != "sem_amostras")  # 2. T02
    assert janela.velocidade_media_kmh == pytest.approx(amostra.velocidade_kmh, rel=1e-12)
    registro = MedicaoContexto.query.one()                                          # 3. registro
    assert registro.dados["velocidade_media_kmh"] == janela.velocidade_media_kmh
    corpo = get(app, trechos()[0].id).get_json()                                    # 4. endpoint
    item = corpo["series"][0]["janelas"][0]
    assert item["velocidade_media_kmh"] == registro.dados["velocidade_media_kmh"]
    assert item["velocidade_media_kmh"] == pytest.approx(36.7558, rel=1e-4)
    assert (item["inicio"], item["fim"]) == ("2026-11-24T12:00:00Z", "2026-11-24T12:15:00Z")
    assert item["calculo_amostras"] == [amostra.versao_calculo]
    assert item["amostras_validas"] == 1 and item["amostras_descartadas"] == 0


def test_resposta_tem_trecho_versao_fonte_metodo_qualidade_e_janelas_explicitas(app, cenario):
    viagem(cenario, 0, "5810", (0, "A"), (30, "B"), (90, "C"))
    agregar_banco(cenario)
    ab = trechos()[0]
    resposta = get(app, ab.id)  # sem login nem cabeçalho de autenticação
    assert resposta.status_code == 200 and "X-RateLimit-Limit" in resposta.headers
    corpo = resposta.get_json()
    assert corpo["trecho"] == {"id": ab.id, "versao_gtfs_id": cenario[0].id, "linha": "3762-10",
                               "sentido": "0", "ordem": 1}
    assert corpo["fonte"]["codigo"] == FONTE_AGREGADO == "VELOCIDADE_OBSERVADA_ONIBUS"
    assert corpo["fonte"]["tipo"] == "PROXY_TRAFEGO_VELOCIDADE_ONIBUS"
    assert "não é a velocidade" in corpo["fonte"]["descricao"]
    assert corpo["fonte"]["politica"]["waze_integrado"] is False
    assert corpo["unidades"]["velocidade_media_kmh"] == "km/h" and corpo["motivo"] is None
    serie, = corpo["series"]
    assert serie["versao_fonte"] == "us08-t02-v1-mediaARITMETICA-INSTANTE_ORIGEM"
    assert (serie["metodo_media"], serie["atribuicao_janela"]) == ("ARITMETICA", "INSTANTE_ORIGEM")
    assert serie["versoes"] == {"agregado": serie["versao_fonte"], "correspondencia": "match-sint",
                                "estimativa": VERSAO_EST, "gtfs": cenario[0].id, "reconstrucao": "recon-sint",
                                "calculo": "us08-t01-v1-lacuna120-vmin1-vmax80"}
    assert [j["inicio"][11:16] for j in serie["janelas"]] == ["12:00", "12:15", "12:30"]
    cheia, vazia, _ = serie["janelas"]
    assert cheia["cobertura"] == {"proporcao_validas": 1.0}
    assert set(cheia["qualidade_coleta"]) == {"situacao", "motivo", "percentual_falhas", "incompleto"}
    assert (vazia["velocidade_media_kmh"], vazia["motivo"], vazia["amostras_validas"]) == (
        None, "sem_registro", 0)


def test_janela_sem_amostras_validas_e_nula_com_motivo(app, cenario):
    viagem(cenario, 0, "5810", (0, "A"), (30, "B"))
    agregar_banco(cenario, limites=Limites(LACUNA))  # plausibilidade indefinida → PENDENTE
    item = get(app, trechos()[0].id).get_json()["series"][0]["janelas"][0]
    assert (item["velocidade_media_kmh"], item["motivo"]) == (None, "amostras_pendentes")
    assert (item["amostras_validas"], item["amostras_descartadas"]) == (0, 1)
    assert item["descartes_por_motivo"] == {"plausibilidade_indefinida": 1}


def test_metodo_indefinido_devolve_nulo_com_candidatos(app, cenario):
    viagem(cenario, 0, "5810", (0, "A"), (30, "B"))
    agregar_banco(cenario, metodo=None)
    item = get(app, trechos()[0].id).get_json()["series"][0]["janelas"][0]
    assert (item["velocidade_media_kmh"], item["motivo"]) == (None, "metodo_media_indefinido")
    assert item["candidatos_kmh"]["ARITMETICA"] == pytest.approx(36.7558, rel=1e-4)


def test_trecho_sem_resultado_persistido(app, cenario):
    corpo = get(app, trechos()[1].id).get_json()
    assert (corpo["series"], corpo["motivo"]) == ([], "sem_resultados_persistidos")


def test_nao_mistura_trechos_metodos_nem_reconstrucoes(app, cenario):
    viagem(cenario, 0, "5810", (0, "A"), (30, "B"), (90, "C"))
    viagem(cenario, 1, "5811", (0, "A"), (60, "B"), reconstrucao="recon-outra")
    agregar_banco(cenario)
    agregar_banco(cenario, metodo="ESPACIAL")
    agregar_banco(cenario, versao_reconstrucao="recon-outra")
    ab, bc = trechos()
    series = get(app, ab.id).get_json()["series"]
    chaves = {(s["metodo_media"], s["versoes"]["reconstrucao"]) for s in series}
    assert chaves == {("ARITMETICA", "recon-sint"), ("ESPACIAL", "recon-sint"),
                      ("ARITMETICA", "recon-outra")}
    outra = next(s for s in series if s["versoes"]["reconstrucao"] == "recon-outra")
    assert [j["motivo"] for j in outra["janelas"]] == ["sem_registro", None, "sem_registro"]
    filtrada = get(app, ab.id, versao_fonte="us08-t02-v1-mediaESPACIAL-INSTANTE_ORIGEM").get_json()
    assert [s["metodo_media"] for s in filtrada["series"]] == ["ESPACIAL"]
    # B→C só tem a viagem recon-sint; nada do trecho A→B aparece nele.
    assert {s["versoes"]["reconstrucao"] for s in get(app, bc.id).get_json()["series"]} == {"recon-sint"}


def test_periodo_devolve_so_janelas_inteiras(app, cenario):
    viagem(cenario, 0, "5810", (0, "A"), (30, "B"))  # janela 12:00
    agregar_banco(cenario)
    parcial = dict(inicio=T0 + timedelta(minutes=1), fim=T0 + timedelta(minutes=31))
    # 12:00–12:15 não cabe inteira em [12:01, 12:31): o registro dela não entra.
    assert get(app, trechos()[0].id, **parcial).get_json()["motivo"] == "sem_resultados_persistidos"
    viagem(cenario, 1, "5811", (0, "A"), (30, "B"))  # janela 12:15
    agregar_banco(cenario)
    corpo = get(app, trechos()[0].id, **parcial).get_json()
    assert [j["inicio"][11:16] for j in corpo["series"][0]["janelas"]] == ["12:15"]
    assert janelas_inteiras(T0, T0 + timedelta(minutes=15)) == [T0]


@pytest.mark.parametrize("consulta, campo", [
    ({"inicio": "2026-11-24T12:00:00Z", "fim": "2026-11-24T13:00:00Z"}, "trecho_id"),
    ({"trecho_id": "x", "fim": "2026-11-24T13:00:00Z"}, "inicio"),
    ({"trecho_id": "x", "inicio": "2026-11-24T12:00:00", "fim": "2026-11-24T13:00:00Z"}, "inicio"),
    ({"trecho_id": "x", "inicio": "2026-11-24T13:00:00Z", "fim": "2026-11-24T12:00:00Z"}, "fim"),
    ({"trecho_id": "x", "inicio": "2026-10-01T00:00:00Z", "fim": "2026-11-24T00:00:00Z"}, "fim"),
    ({"trecho_id": "x", "inicio": "2026-11-24T12:00:00Z", "fim": "2026-11-24T13:00:00Z",
      "fonte": "WAZE"}, "fonte"),
    ({"trecho_id": "x", "inicio": "2026-11-24T12:00:00Z", "fim": "2026-11-24T13:00:00Z",
      "linha": "1"}, "linha"),
    ({"trecho_id": "x", "inicio": "2026-11-24T12:00:00Z", "fim": "2026-11-24T13:00:00Z",
      "versao_fonte": "v" * 129}, "versao_fonte"),
])
def test_parametros_invalidos(app, consulta, campo):
    resposta = app.test_client().get("/contexto/trafego", query_string=consulta)
    assert resposta.status_code == 400
    assert resposta.get_json()["erro"]["campo"] == campo


def test_trecho_inexistente(app):
    assert get(app, "nao-existe").status_code == 404


def test_servico_reutilizavel_sem_http_e_sem_rede(app, cenario, sem_rede):
    """US21 pode ler o mesmo resultado pelo QueryService, sem passar por HTTP."""
    viagem(cenario, 0, "5810", (0, "A"), (30, "B"))
    agregar_banco(cenario)
    corpo = consultar_trafego_trecho(trechos()[0].id, T0, FIM)
    assert corpo == get(app, trechos()[0].id).get_json()
    assert MedicaoContexto.query.count() == 1  # a consulta não grava nem recalcula
