"""US18-T01/T02/T03A com dados SINTÉTICOS (linha 3762-10, 24/11/2026).

Mesmo cenário da US17: parada B, passagens 12:00, 12:04, 12:10, 12:13 e 12:16 UTC;
programação por frequência com partidas 12:00, 12:15 e 12:30 (H = 300 s).

Conta manual da janela 12:00–12:15 (intervalos 240, 360, 180 s):
  Σh = 780; Σh² = 57600 + 129600 + 32400 = 219600
  E = 219600 ÷ 1560 = 140,769231 s = 2,346154 min
  vigente: partida 12:00 (H = 300 s) → E_prog = 150 s = 2,5 min
  excesso = 140,769231 − 150 = −9,230769 s = −0,153846 min (negativo mantido)
Faixa 09:00–09:30 local (240, 360, 180, 180 s): Σh = 960; Σh² = 252000;
  E = 131,25 s = 2,1875 min; vigentes 12:00 e 12:15 (300 s cada) → 150 s;
  excesso = −18,75 s = −0,3125 min.
"""

from datetime import date, timedelta

import pytest

from app import db
from app.indicadores.espera import (
    ESPERA, EXCESSO, UNIDADE, calcular_e_publicar_espera, calcular_espera, espera_estimada, espera_programada,
)
from app.models import PartidaProgramada
from test_regularidade_us17 import (  # noqa: F401 (fixtures)
    DEPOIS, MANUAL, MATCH, RECON, T0, cenario, frequencia, lacuna_de_teste, passar,
)


def calcular(c):
    return calcular_espera(linha="3762-10", sentido="0", data_operacao=date(2026, 11, 24),
                           versao_gtfs_id=c["versao"].id, versao_correspondencia=MATCH,
                           versao_reconstrucao=RECON, agora=DEPOIS, intervalo=60)


def janela(campos, indicador, minuto, parada="B"):
    (item,) = [x for x in campos if (x["indicador"], x["parada_codigo"], x["periodo_inicio"])
               == (indicador, parada, T0 + timedelta(minutes=minuto))]
    return item


# --- US18-T01 ------------------------------------------------------------------------

def test_espera_confere_com_a_conta_manual_e_nulos():
    espera, motivo = espera_estimada([240, 360, 180])
    assert (espera, motivo) == (pytest.approx(219600 / 1560), None)
    assert espera_estimada([300, 300]) == (150, None)  # intervalos iguais: H ÷ 2
    assert espera_estimada([300]) == (None, "passagens_insuficientes")
    assert espera_estimada([]) == (None, "passagens_insuficientes")
    assert espera_estimada([0, 0]) == (None, "divisor_invalido")


def test_janela_publica_espera_em_minutos_com_os_intervalos_da_us17(frequencia):
    passar(frequencia, "B", MANUAL)
    campos = calcular(frequencia)
    espera = janela(campos, ESPERA, 0)
    assert (espera["valor"], espera["unidade"]) == (pytest.approx(2.346154, abs=1e-6), UNIDADE)
    assert espera["qualidade"]["intervalos_seg"] == [240.0, 360.0, 180.0]
    assert espera["versao_calculo"] == "espera-1.0" and espera["versao_conjunto"]
    insuficiente = janela(campos, ESPERA, 15)
    assert (insuficiente["valor"], insuficiente["qualidade"]["motivo"]) == (None, "passagens_insuficientes")


# --- US18-T02 ------------------------------------------------------------------------

def test_excesso_negativo_mantido_com_programacao_usada(frequencia):
    passar(frequencia, "B", MANUAL)
    excesso = janela(calcular(frequencia), EXCESSO, 0)
    assert excesso["valor"] == pytest.approx(-0.153846, abs=1e-6)  # não truncado em zero
    q = excesso["qualidade"]
    assert q["espera_programada_min"] == 2.5
    assert q["intervalos_programados"] == [
        {"partida_id": frequencia["partidas"][0].id, "intervalo_seg": 300}]
    assert q["captura_programacao"]["versao_fonte"] == "captura-1"
    assert "sem truncar" in q["regra_excesso"] and q["situacao"] == "CALCULADA"


def test_janela_que_cruza_faixas_usa_regra_registrada(frequencia):
    db.session.add(PartidaProgramada(importacao=frequencia["importacao"], ordem=99,
                                     saida_prevista=T0 + timedelta(minutes=5), tipo_oferta="FREQUENCIA",
                                     intervalo_seg=600))
    db.session.commit()
    assert espera_programada([300, 600]) == pytest.approx((300 ** 2 + 600 ** 2) / 1800)  # 250 s
    assert espera_programada([300]) == 150 and espera_programada([]) is None
    passar(frequencia, "B", MANUAL)
    excesso = janela(calcular(frequencia), EXCESSO, 0)
    assert excesso["qualidade"]["espera_programada_min"] == pytest.approx(250 / 60, abs=1e-6)
    assert excesso["valor"] == pytest.approx((219600 / 1560 - 250) / 60, abs=1e-6)


def test_ausencia_de_intervalo_programado_fica_explicita(frequencia):
    passar(frequencia, "B", (45, 50, 55))  # 12:45–13:00: nenhuma partida vigente
    campos = calcular(frequencia)
    espera, excesso = janela(campos, ESPERA, 45), janela(campos, EXCESSO, 45)
    assert espera["valor"] == pytest.approx(2.5)  # intervalos 300 e 300 s → E = 150 s
    assert (excesso["valor"], excesso["qualidade"]["motivo"]) == (None, "intervalo_programado_ausente")


# --- US18-T03A -----------------------------------------------------------------------

def consultar(app, **params):
    resposta = app.test_client().get("/indicadores", query_string={
        "tipo": "ESPERA", "linha": "3762-10", "sentido": "ida", "parada": "B", **params})
    assert resposta.status_code == 200, resposta.get_json()
    return resposta.get_json()["dados"]


def test_api_separa_espera_excesso_e_ausencia(frequencia, app):
    passar(frequencia, "B", MANUAL)
    calcular_e_publicar_espera(linha="3762-10", sentido="0", data_operacao=date(2026, 11, 24),
                               versao_gtfs_id=frequencia["versao"].id, versao_correspondencia=MATCH,
                               versao_reconstrucao=RECON, agora=DEPOIS, intervalo=60)
    (item,) = consultar(app, inicio=T0.isoformat(), fim=T0.isoformat())
    assert (item["parada"], item["inicio"], item["fim"]) == (
        "B", "2026-11-24T12:00:00+00:00", "2026-11-24T12:15:00+00:00")
    assert item["espera_estimada"]["valor"] == pytest.approx(2.346154, abs=1e-6)
    assert item["excesso_espera"]["valor"] == pytest.approx(-0.153846, abs=1e-6)
    assert item["espera_estimada"]["unidade"] == item["excesso_espera"]["unidade"] == "min"
    assert item["excesso_espera"]["espera_programada_min"] == 2.5

    (sem,) = consultar(app, inicio=(T0 + timedelta(minutes=15)).isoformat(),
                       fim=(T0 + timedelta(minutes=15)).isoformat())
    for lado in ("espera_estimada", "excesso_espera"):
        assert sem[lado]["valor"] is None and sem[lado]["valor"] != 0
        assert sem[lado]["motivo_ausencia"] == "passagens_insuficientes"

    (faixa,) = consultar(app, agregacao="faixa", hora_inicio="09:00", hora_fim="09:30",
                         inicio="2026-11-24T00:00:00-03:00", fim="2026-11-25T00:00:00-03:00")
    assert faixa["espera_estimada"]["valor"] == pytest.approx(2.1875)
    assert faixa["excesso_espera"]["valor"] == pytest.approx(-0.3125)
    assert faixa["excesso_espera"]["intervalos_programados_seg"] == [300, 300]
