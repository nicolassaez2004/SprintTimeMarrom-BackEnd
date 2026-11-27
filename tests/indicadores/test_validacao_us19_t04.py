"""Caderno de referência US19-T04, sem banco compartilhado ou aceite fictício."""
from copy import deepcopy

import pytest

from scripts.validar_us19_t04 import carregar_caderno, executar_caso

CADERNO = carregar_caderno()


@pytest.mark.parametrize('caso', CADERNO['casos'], ids=lambda c: c['id'])
def test_caso_manual_controlado(caso):
    resultado = executar_caso(caso, CADERNO)
    assert resultado['resultado'] == 'CONFERE_NO_ENSAIO'


def test_oraculo_independente_detecta_sinal_invertido():
    caso = deepcopy(CADERNO['casos'][0])
    caso['esperado']['variacao'] = -180
    with pytest.raises(AssertionError):
        executar_caso(caso, CADERNO)
