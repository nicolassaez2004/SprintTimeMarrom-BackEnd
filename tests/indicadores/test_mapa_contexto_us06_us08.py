"""Clima (US06) e trânsito (US08) como opções de visualização do mapa ao vivo (US22).

GET /mapas/clima e GET /mapas/trafego devolvem GeoJSON num instante. Dados sintéticos de
teste (SQLite em memória); nada é gravado fora do teste.
"""
from datetime import timedelta
from decimal import Decimal

from app import db
from app.models import EstacaoMeteorologica, ObservacaoClima, Trecho
from tests.indicadores.test_velocidade_15min_us08_t02 import (  # noqa: F401  (fixture)
    T0, agregar_banco, cenario, viagem,
)


def iso(instante):
    return instante.isoformat().replace("+00:00", "Z")


def estacao(codigo, lat, lon, fonte="INMET", intervalo=3600):
    e = EstacaoMeteorologica(fonte=fonte, codigo=codigo, nome=f"Estação {codigo}", latitude=Decimal(str(lat)),
                             longitude=Decimal(str(lon)), intervalo_seg=intervalo, versao_fonte="teste")
    db.session.add(e)
    db.session.flush()
    return e


def observar(e, instante, chuva, temperatura):
    db.session.add(ObservacaoClima(estacao_id=e.id, instante=instante, intervalo_seg=e.intervalo_seg,
                                   chuva=chuva, temperatura=temperatura, fonte=e.fonte, versao_fonte="teste"))


# --- Clima -----------------------------------------------------------------------------------

def test_clima_mostra_cada_estacao_com_a_ultima_janela_terminada(app):
    mirante, interlagos = estacao("A701", -23.4962, -46.6201), estacao("A771", -23.7244, -46.6775)
    observar(mirante, T0, Decimal("2.4"), Decimal("21.5"))           # janela (11:00, 12:00]
    observar(mirante, T0 + timedelta(hours=1), Decimal("9"), Decimal("19"))  # ainda não terminou às 12:40
    db.session.commit()
    resposta = app.test_client().get("/mapas/clima", query_string={"instante": iso(T0 + timedelta(minutes=40))})
    assert resposta.status_code == 200 and resposta.mimetype == "application/geo+json"
    corpo = resposta.get_json()
    assert corpo["janela"] == {"inicio": "2026-11-24T11:00:00Z", "fim": "2026-11-24T12:00:00Z", "intervalo_seg": 3600}
    por_codigo = {f["properties"]["codigo"]: f for f in corpo["features"]}
    assert por_codigo["A701"]["geometry"] == {"type": "Point", "coordinates": [-46.6201, -23.4962]}
    assert (por_codigo["A701"]["properties"]["chuva_mm"], por_codigo["A701"]["properties"]["temperatura_c"]) == (2.4, 21.5)
    # Sem observação: null com motivo, nunca chuva zero.
    assert por_codigo["A771"]["properties"]["chuva_mm"] is None
    assert por_codigo["A771"]["properties"]["motivo"] == "sem_observacao"
    assert corpo["unidades"] == {"chuva": "mm", "temperatura": "°C"}


def test_clima_separa_fontes_e_avisa_quando_nao_ha_estacoes(app):
    estacao("CGE-1", -23.55, -46.63, fonte="CGE", intervalo=900)
    db.session.commit()
    cliente = app.test_client()
    inmet = cliente.get("/mapas/clima", query_string={"instante": iso(T0)}).get_json()
    assert inmet["features"] == [] and inmet["motivo"] == "sem_estacoes"
    cge = cliente.get("/mapas/clima", query_string={"instante": iso(T0 + timedelta(minutes=7)), "fonte": "cge"}).get_json()
    assert [f["properties"]["codigo"] for f in cge["features"]] == ["CGE-1"]
    assert cge["janela"]["fim"] == "2026-11-24T12:00:00Z" and cge["janela"]["intervalo_seg"] == 900


def test_clima_valida_parametros(app):
    cliente = app.test_client()
    for consulta, campo in (({}, "instante"), ({"instante": "2026-11-24 12:00"}, "instante"),
                            ({"instante": "2026-11-24T12:00:00"}, "instante"),          # sem fuso
                            ({"instante": iso(T0), "fonte": "WAZE"}, "fonte"),
                            ({"instante": iso(T0), "x": "1"}, "x")):
        resposta = cliente.get("/mapas/clima", query_string=consulta)
        assert resposta.status_code == 400 and resposta.get_json()["erro"]["campo"] == campo, consulta


# --- Trânsito --------------------------------------------------------------------------------

def test_transito_mostra_os_trechos_da_linha_com_a_velocidade_da_janela(app, cenario):
    viagem(cenario, 0, "5810", (0, "A"), (30, "B"))
    agregar_banco(cenario)
    resposta = app.test_client().get("/mapas/trafego", query_string={
        "linha": "3762-10", "instante": iso(T0 + timedelta(minutes=10))})
    assert resposta.status_code == 200 and resposta.mimetype == "application/geo+json"
    corpo = resposta.get_json()
    assert corpo["janela"] == {"inicio": "2026-11-24T12:00:00Z", "fim": "2026-11-24T12:15:00Z"}
    assert corpo["versao_gtfs_id"] == cenario[0].id and corpo["motivo"] is None
    ab, bc = corpo["features"]
    assert ab["geometry"]["type"] == "LineString" and ab["geometry"]["coordinates"][0] == [-46.7, -23.55]
    assert ab["properties"]["ordem"] == 1 and ab["properties"]["velocidade_media_kmh"] > 0
    # Trecho sem registro na janela: null com motivo, nunca zero.
    assert bc["properties"]["velocidade_media_kmh"] is None and bc["properties"]["motivo"]
    assert corpo["fonte"]["tipo"] == "PROXY_TRAFEGO_VELOCIDADE_ONIBUS"


def test_transito_sem_versao_vigente_ou_linha_sem_trechos(app, cenario):
    cliente = app.test_client()
    fora = cliente.get("/mapas/trafego", query_string={"linha": "3762-10", "instante": "2027-06-01T12:00:00Z"})
    assert fora.get_json()["motivo"] == "sem_versao_gtfs_vigente" and fora.get_json()["features"] == []
    outra = cliente.get("/mapas/trafego", query_string={"linha": "9999-10", "instante": iso(T0)})
    assert outra.get_json()["motivo"] == "linha_sem_trechos"
    so_volta = cliente.get("/mapas/trafego", query_string={"linha": "3762-10", "sentido": "1", "instante": iso(T0)})
    assert so_volta.get_json()["motivo"] == "linha_sem_trechos"
    assert Trecho.query.count() == 2  # a consulta só lê


def test_o_mapa_do_site_pode_consultar_as_duas_rotas(app):
    cliente = app.test_client()
    origem = {"Origin": "http://127.0.0.1:5173"}
    for caminho, consulta in (("/mapas/clima", {"instante": iso(T0)}),
                              ("/mapas/trafego", {"instante": iso(T0), "linha": "3762-10"})):
        resposta = cliente.get(caminho, query_string=consulta, headers=origem)
        assert resposta.headers.get("Access-Control-Allow-Origin") == "http://127.0.0.1:5173", caminho


def test_transito_valida_parametros(app):
    cliente = app.test_client()
    for consulta, campo in (({"instante": iso(T0)}, "linha"), ({"linha": "x" * 65, "instante": iso(T0)}, "linha"),
                            ({"linha": "3762-10"}, "instante"),
                            ({"linha": "3762-10", "instante": iso(T0), "sentido": "2"}, "sentido")):
        resposta = cliente.get("/mapas/trafego", query_string=consulta)
        assert resposta.status_code == 400 and resposta.get_json()["erro"]["campo"] == campo, consulta
