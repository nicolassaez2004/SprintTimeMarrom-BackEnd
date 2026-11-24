"""US18-T03B: vinte casos CONTROLADOS de espera e excesso conferidos pela API."""

from collections import Counter
import json

import pytest

from scripts.validar_us18_t03b import CADERNO, RAIZ, executar, hash_texto


@pytest.fixture(scope="module")
def conferencia():
    return executar()


@pytest.mark.parametrize("identificador", [f"C{i:02}" for i in range(1, 21)])
def test_caso_confere_com_calculo_manual(conferencia, identificador):
    caso = next(c for c in conferencia["casos"] if c["id"] == identificador)
    assert caso["status"] == "CONFERIU_EXEMPLO", {
        "erro": caso.get("erro"), "divergencias": [c for c in caso.get("comparacoes", []) if not c["confere"]]}


def test_quatro_escalas_e_aceite_externo_dispensado(conferencia):
    assert conferencia["resumo"] == {"CONFERIU_EXEMPLO": 20}
    assert Counter(c["escala"] for c in conferencia["casos"]) == {"janela": 5, "faixa": 5, "dia": 5, "mes": 5}
    assert conferencia["aceite_institucional"] == "DISPENSADO_PELO_RESPONSAVEL"
    assert conferencia["pronto"] is True


def test_evidencia_guardada_corresponde_ao_caderno():
    evidencia = json.loads((RAIZ / "docs/evidencias/US18-T03B-conferencia.json").read_text(encoding="utf-8"))
    assert evidencia["sha256_caderno"] == hash_texto(CADERNO)
    assert evidencia["resumo"] == {"CONFERIU_EXEMPLO": 20}
