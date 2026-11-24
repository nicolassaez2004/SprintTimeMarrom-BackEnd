"""Casos sintéticos de fronteira; não substituem validação da instituição."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.indicadores.cumprimento import (
    calcular_cumprimento,
    viagem_cumprida,
)

UTC = timezone.utc
BASE = datetime(2026, 11, 24, 12, tzinfo=UTC)


def partida(*, minuto=0, ordem=1, tipo="FREQUENCIA", intervalo=1800, grade="g1", id=None):
    return SimpleNamespace(
        id=id or f"p{ordem}", importacao_id=grade, ordem=ordem,
        saida_prevista=BASE + timedelta(minutes=minuto),
        tipo_oferta=tipo, intervalo_seg=intervalo,
    )


def viagem(*, cobertura=0.8, segundos=0, partida_prevista=None):
    partida_prevista = partida_prevista or partida()
    return SimpleNamespace(
        partida_programada_id=partida_prevista.id,
        cobertura_tracado=cobertura,
        saida_observada=partida_prevista.saida_prevista + timedelta(seconds=segundos),
    )


# Vinte cálculos conferíveis manualmente. Devem ser levados ao PO/instituição;
# resultados sintéticos não equivalem a aceite externo.
@pytest.mark.parametrize("cobertura,offset,intervalo,esperado", [
    (0.80, 0, 1800, True),
    (0.7999, 0, 1800, False),
    (1.00, 900, 1800, True),
    (1.00, -900, 1800, True),
    (0.90, 901, 1800, False),
    (0.90, -901, 1800, False),
    (0.80, 900, 1800, True),
    (0.80, 901, 1800, False),
    (0.90, 300, 600, True),
    (0.90, 301, 600, False),
    (0.90, -300, 600, True),
    (0.90, -301, 600, False),
    (0.95, 900, 3600, True),  # tolerância limitada a 15 min
    (0.95, 901, 3600, False),
    (0.85, 600, 1200, True),
    (0.85, 601, 1200, False),
    (0.85, 30, 60, True),
    (0.85, 31, 60, False),
    (1.00, 0, 1, True),
    (0.79, -1, 1800, False),
])
def test_vinte_casos_de_fronteira(cobertura, offset, intervalo, esperado):
    programada = partida(intervalo=intervalo)
    assert viagem_cumprida(programada, viagem(
        cobertura=cobertura, segundos=offset, partida_prevista=programada,
    ), [programada]) is esperado


def test_horario_fixo_inferido_da_partida_vizinha_na_mesma_grade():
    programadas = [
        partida(minuto=0, ordem=1, tipo="FIXA", intervalo=None),
        partida(minuto=10, ordem=2, tipo="FIXA", intervalo=None),
        partida(minuto=35, ordem=3, tipo="FIXA", intervalo=None),
    ]
    central = programadas[1]
    # Menor intervalo adjacente: 10 min; tolerância é ±5 min.
    assert viagem_cumprida(central, viagem(segundos=300, partida_prevista=central), programadas)
    assert not viagem_cumprida(central, viagem(segundos=301, partida_prevista=central), programadas)


def test_horario_fixo_nao_infere_intervalo_entre_grades():
    central = partida(tipo="FIXA", intervalo=None, grade="grade-a")
    vizinha_outra_grade = partida(minuto=10, tipo="FIXA", intervalo=None,
                                  grade="grade-b", id="outra")
    assert not viagem_cumprida(central, viagem(partida_prevista=central),
                               [central, vizinha_outra_grade])


def test_cobertura_exatamente_80_porcento_e_contada():
    programadas = [partida(id="a"), partida(minuto=30, ordem=2, id="b")]
    observadas = {"a": viagem(partida_prevista=programadas[0]),
                  "b": viagem(cobertura=0.799, partida_prevista=programadas[1])}
    resultado = calcular_cumprimento(programadas, observadas)
    assert (resultado.cumpridas, resultado.programadas) == (1, 2)
    assert resultado.percentual == Decimal("50.00")


def test_denominador_zero_preserva_valor_null():
    resultado = calcular_cumprimento([], [])
    assert (resultado.cumpridas, resultado.programadas, resultado.percentual) == (0, 0, None)
