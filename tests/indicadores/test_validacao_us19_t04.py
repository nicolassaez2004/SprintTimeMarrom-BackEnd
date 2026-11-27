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


def test_recorte_da_escala_e_do_sistema_e_respeita_bordas_e_linha():
    """Revisão US19-T04: o roteiro não filtra pares; o recorte é ``recortar_correlacao``."""
    from dataclasses import replace
    from datetime import datetime, timedelta

    from app.indicadores.propagacao import correlacionar_chegada_partida, recortar_correlacao
    from scripts.validar_us19_t04 import LINHA, REDE, ambiente_controlado, vv

    with ambiente_controlado():
        viagens = []
        for nome, chegada, linha in (("p1", "09:00", LINHA), ("p2", "09:15", LINHA), ("p3", "09:05", "outra")):
            t = datetime.fromisoformat(f"2026-11-24T{chegada}:00-03:00")
            a = replace(vv(nome + "-a", 0, 20, ac=60, prefixos=(nome,)), saida=t - timedelta(minutes=20),
                        chegada=t, termino=t, linha_codigo=linha)
            b = replace(vv(nome + "-b", 25, 35, ap=60, prefixos=(nome,)), saida=t + timedelta(minutes=5),
                        chegada=t + timedelta(minutes=15), termino=t + timedelta(minutes=15), linha_codigo=linha)
            viagens += [a, b]
        todos = correlacionar_chegada_partida(viagens, timedelta(minutes=30))
        recorte = recortar_correlacao(todos, linha=LINHA, versao_gtfs_id=REDE,
                                      inicio=datetime.fromisoformat("2026-11-24T09:00:00-03:00"),
                                      fim=datetime.fromisoformat("2026-11-24T09:15:00-03:00"))
        assert [p.anterior.viagem_id for p in recorte.pares] == ["p1-a"]  # 09:15 e outra linha ficam fora
        assert recorte.coeficiente is None and recorte.motivo_sem_valor == "amostra_insuficiente"
