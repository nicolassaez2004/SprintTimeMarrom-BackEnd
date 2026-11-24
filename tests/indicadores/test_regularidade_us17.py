"""US17-T01/T02/T03A com dados SINTÉTICOS (linha 3762-10, 24/11/2026).

Reaproveita o cenário da US15-T02 (percurso A→B→C→D, coleta confiável de
12:00 a 13:00 UTC = 09:00 a 10:00 em São Paulo) com a oferta por frequência.

Exemplo manual, parada B, sentido ida (horários UTC):
passagens 12:00, 12:04, 12:10, 12:13 e 12:16.
- Janela 12:00–12:15: intervalos 240, 360 e 180 s (o de cada passagem posterior).
  média 260; desvio populacional √((20² + 100² + 80²)/3) = √5600 = 74,8331;
  CV = 0,287820.
- Janela 12:15–12:30: um intervalo (180 s) → NULL, passagens insuficientes.
- Faixa 09:00–09:30 local: 240, 360, 180, 180 → média 240; √5400 = 73,4847;
  CV = 0,306186.
"""

from datetime import date, timedelta
from math import sqrt

import pytest

from app import db
from app.indicadores.regularidade import (
    LIMITE_AGRUPAMENTO, REGULARIDADE, UNIDADE, agrupamento, calcular_e_publicar_regularidade,
    calcular_regularidade, coeficiente_variacao, intervalos_sucessivos,
)
from app.models import EstimativaPassagemViagem, ImportacaoProgramacao, PartidaProgramada, PassagemEstimada
from test_frequencia_us15_t02 import (  # noqa: F401 (fixtures)
    DEPOIS, MATCH, RECON, T0, VERSAO, cenario, lacuna_de_teste, passagem,
)

UTC = T0.tzinfo
MANUAL = (0, 4, 10, 13, 16)  # minutos após 12:00 UTC


@pytest.fixture
def frequencia(cenario):
    """Programação da data passa a ser por frequência (intervalo previsto de 5 min)."""
    for partida in cenario["partidas"]:
        partida.tipo_oferta, partida.intervalo_seg = "FREQUENCIA", 300
    db.session.commit()
    return cenario


def partida(c, minuto, tipo="FREQUENCIA", importacao=None):
    c["seq"][0] += 1
    return PartidaProgramada(importacao=importacao or c["apoio"], ordem=1000 + c["seq"][0],
                             saida_prevista=T0 + timedelta(minutes=minuto), tipo_oferta=tipo,
                             intervalo_seg=300 if tipo == "FREQUENCIA" else None)


def passar(c, parada, minutos, **kw):
    for m in minutos:
        passagem(c, parada, T0 + timedelta(minutes=m), partida=partida(c, m, **kw))


def calcular(c, **extra):
    return calcular_regularidade(**{**dict(
        linha="3762-10", sentido="0", data_operacao=date(2026, 11, 24), versao_gtfs_id=c["versao"].id,
        versao_correspondencia=MATCH, versao_reconstrucao=RECON, agora=DEPOIS, intervalo=60), **extra})


def janela(campos, parada, minuto):
    (item,) = [x for x in campos if (x["parada_codigo"], x["periodo_inicio"])
               == (parada, T0 + timedelta(minutes=minuto))]
    return item


# --- US17-T01 ----------------------------------------------------------------------

def test_exemplo_manual_produz_os_intervalos_esperados(frequencia):
    passar(frequencia, "B", MANUAL)
    campos = calcular(frequencia)
    primeira = janela(campos, "B", 0)
    assert primeira["qualidade"]["intervalos_seg"] == [240.0, 360.0, 180.0]
    assert janela(campos, "B", 15)["qualidade"]["intervalos_seg"] == [180.0]
    assert "passagem posterior" in primeira["qualidade"]["regra_fronteira"]
    assert intervalos_sucessivos([T0 + timedelta(minutes=m) for m in (10, 0, 4)]) == [
        (T0, T0 + timedelta(minutes=4), 240.0),
        (T0 + timedelta(minutes=4), T0 + timedelta(minutes=10), 360.0)]


def test_sentidos_linhas_e_ofertas_diferentes_nao_se_misturam(frequencia):
    passar(frequencia, "B", MANUAL)
    # Volta da mesma linha, na mesma parada e nos mesmos minutos intermediários.
    volta = ImportacaoProgramacao(
        versao_gtfs_id=frequencia["versao"].id, linha_codigo="3762-10", data_operacao=date(2026, 11, 24),
        sentido="1", tipo_dia="UTIL", fonte="GTFS", versao_fonte="volta", fonte_url="gtfs://volta",
        capturada_em=T0, verificada_em=T0, arquivo=frequencia["importacao"].arquivo)
    db.session.add(volta)
    db.session.commit()
    passar(frequencia, "B", (2, 6, 8), importacao=volta)
    # Viagens da oferta FIXA não entram nos intervalos da oferta por frequência.
    passar(frequencia, "B", (1, 7), tipo="FIXA")
    assert janela(calcular(frequencia), "B", 0)["qualidade"]["intervalos_seg"] == [240.0, 360.0, 180.0]


def test_duplicacao_da_mesma_evidencia_gps_e_tratada_antes_do_calculo(frequencia):
    passar(frequencia, "B", MANUAL)
    original = PassagemEstimada.query.filter_by(parada_codigo="B").order_by(PassagemEstimada.instante).all()[1]
    viagem = passagem(frequencia, "C", T0 + timedelta(minutes=30), partida=partida(frequencia, 30))
    db.session.add(PassagemEstimada(
        viagem=viagem, versao_gtfs_id=original.versao_gtfs_id, parada_codigo="B",
        instante=original.instante, metodo=original.metodo, versao_estimativa=original.versao_estimativa,
        posicao_anterior_id=original.posicao_anterior_id, posicao_posterior_id=original.posicao_posterior_id))
    db.session.commit()
    assert janela(calcular(frequencia), "B", 0)["qualidade"]["intervalos_seg"] == [240.0, 360.0, 180.0]


# --- US17-T02 ----------------------------------------------------------------------

def test_cv_com_convencao_registrada_e_limite_de_agrupamento():
    assert coeficiente_variacao([240, 360, 180]) == (pytest.approx(sqrt(5600) / 260), None)
    # Limite exato 0,5 não é agrupamento; acima dele é.
    cv, _ = coeficiente_variacao([100, 300])
    assert cv == pytest.approx(0.5) and agrupamento(cv) is False
    cv, _ = coeficiente_variacao([100, 310])
    assert cv > LIMITE_AGRUPAMENTO and agrupamento(cv) is True
    # Insuficiência nunca vira regularidade perfeita (CV 0).
    assert coeficiente_variacao([300]) == (None, "passagens_insuficientes")
    assert coeficiente_variacao([]) == (None, "passagens_insuficientes")
    assert coeficiente_variacao([0, 0]) == (None, "media_invalida")
    assert coeficiente_variacao([300, 300]) == (0.0, None) and agrupamento(0.0) is False
    assert agrupamento(None) is None


def test_janelas_publicam_cv_agrupamento_e_ausencia_com_motivo(frequencia):
    passar(frequencia, "B", MANUAL)
    passar(frequencia, "B", (62, 63, 64))  # após 13:00: coleta sem dados
    campos = calcular(frequencia)
    primeira = janela(campos, "B", 0)
    assert (primeira["indicador"], primeira["unidade"]) == (REGULARIDADE, UNIDADE)
    assert primeira["valor"] == pytest.approx(0.287820, abs=1e-6)
    assert primeira["contagem_numerador"] == 3
    q = primeira["qualidade"]
    assert (q["situacao"], q["agrupamento"], q["media_intervalo_seg"]) == ("CALCULADA", False, 260)
    assert "populacional" in q["convencao"]
    insuficiente = janela(campos, "B", 15)
    assert insuficiente["valor"] is None
    assert insuficiente["qualidade"]["motivo"] == "passagens_insuficientes"
    sem_coleta = janela(campos, "B", 60)
    assert (sem_coleta["valor"], sem_coleta["qualidade"]["motivo"]) == (None, "coleta_sem_dados")
    assert janela(campos, "A", 0)["qualidade"]["motivo"] == "passagens_insuficientes"


def test_parada_sem_evidencia_gps_no_trecho_anula_a_janela(frequencia):
    passar(frequencia, "B", MANUAL)
    # Uma viagem a mais passou por B sem evidência entre 12:06 e 12:08.
    extra = passagem(frequencia, "C", T0 + timedelta(minutes=9), partida=partida(frequencia, 7))
    db.session.get(EstimativaPassagemViagem, (extra.id, VERSAO)).sem_evidencia = [
        {"parada": "B", "inicio": (T0 + timedelta(minutes=6)).isoformat(),
         "fim": (T0 + timedelta(minutes=8)).isoformat()}]
    db.session.commit()
    item = janela(calcular(frequencia), "B", 0)
    assert (item["valor"], item["qualidade"]["motivo"]) == (None, "sem_evidencia_gps")


def test_oferta_por_horario_fixo_nao_tem_regularidade(cenario):
    passar(cenario, "B", MANUAL)
    item = janela(calcular(cenario), "B", 0)
    assert (item["valor"], item["qualidade"]["motivo"]) == (None, "oferta_sem_frequencia")


# --- US17-T03A -----------------------------------------------------------------------

def consultar(app, **params):
    resposta = app.test_client().get("/indicadores", query_string={
        "tipo": REGULARIDADE, "linha": "3762-10", "sentido": "ida", "parada": "B", **params})
    assert resposta.status_code == 200, resposta.get_json()
    return resposta.get_json()["dados"]


def test_api_retorna_regularidade_agrupamento_e_ausencia_sem_zero(frequencia, app):
    passar(frequencia, "B", MANUAL)
    calcular_e_publicar_regularidade(
        linha="3762-10", sentido="0", data_operacao=date(2026, 11, 24), versao_gtfs_id=frequencia["versao"].id,
        versao_correspondencia=MATCH, versao_reconstrucao=RECON, agora=DEPOIS, intervalo=60)

    (com_resultado,) = consultar(app, inicio=T0.isoformat(), fim=T0.isoformat())
    assert com_resultado["valor"] == pytest.approx(0.287820, abs=1e-6)
    assert (com_resultado["unidade"], com_resultado["agrupamento"], com_resultado["motivo_ausencia"]) == (
        UNIDADE, False, None)
    assert com_resultado["intervalos"] == 3 and com_resultado["versao_conjunto"]
    assert com_resultado["versao_calculo"] == "regularidade-1.0"

    (sem_dados,) = consultar(app, inicio=(T0 + timedelta(minutes=15)).isoformat(),
                             fim=(T0 + timedelta(minutes=15)).isoformat())
    assert sem_dados["valor"] is None and sem_dados["valor"] != 0
    assert (sem_dados["agrupamento"], sem_dados["motivo_ausencia"]) == (None, "passagens_insuficientes")

    (faixa,) = consultar(app, agregacao="faixa", hora_inicio="09:00", hora_fim="09:30",
                         inicio="2026-11-24T00:00:00-03:00", fim="2026-11-25T00:00:00-03:00")
    assert faixa["valor"] == pytest.approx(sqrt(5400) / 240, abs=1e-6)
    assert (faixa["intervalos"], faixa["agrupamento"], faixa["referencia"]) == (4, False, "2026-11-24")

    (dia,) = consultar(app, agregacao="dia", inicio="2026-11-24T00:00:00-03:00",
                       fim="2026-11-25T00:00:00-03:00")
    assert dia["valor"] is None  # o dia tem janelas sem coleta: não se afirma CV
    assert dia["motivo_ausencia"].startswith("coleta_")
