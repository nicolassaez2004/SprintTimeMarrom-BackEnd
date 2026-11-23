from datetime import datetime, timedelta, timezone

import pytest

from app.viagens.separacao import ObservacaoViagem, SeparadorViagens


UTC = timezone.utc
D0 = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)


def obs(minuto, *, prefixo="5810", linha="8000-10", sentido="0", valida=True):
    return ObservacaoViagem(prefixo, linha, sentido, D0 + timedelta(minutes=minuto), valida)


def test_separa_continuidade_de_ida():
    resultado = SeparadorViagens().separar([obs(0), obs(5), obs(10)])
    assert len(resultado.viagens) == 1
    viagem = resultado.viagens[0]
    assert (viagem.prefixo, viagem.linha_codigo, viagem.sentido_gtfs) == ("5810", "8000-10", "0")
    assert viagem.inicio == D0 and viagem.fim == D0 + timedelta(minutes=10)
    assert viagem.motivo_encerramento == "fim_da_amostra"


def test_separa_volta_pela_mudanca_de_sentido():
    resultado = SeparadorViagens().separar([obs(0, sentido="0"), obs(5, sentido="0"),
                                           obs(10, sentido="1"), obs(15, sentido="1")])
    assert [(v.sentido_gtfs, v.motivo_encerramento) for v in resultado.viagens] == [
        ("0", "mudanca_sentido"),
        ("1", "fim_da_amostra"),
    ]


def test_lacuna_acima_do_limite_encera_viagem():
    resultado = SeparadorViagens().separar([obs(0), obs(5), obs(21)])
    assert len(resultado.viagens) == 2
    assert resultado.viagens[0].motivo_encerramento == "lacuna"
    assert "lacuna de 16.0 min" in resultado.ambiguidades[0]


def test_linha_mudada_encera_viagem():
    resultado = SeparadorViagens().separar([obs(0, linha="8000-10"), obs(5, linha="8001-10")])
    assert [v.linha_codigo for v in resultado.viagens] == ["8000-10", "8001-10"]
    assert resultado.viagens[0].motivo_encerramento == "mudanca_linha"


def test_observacao_invalida_nao_inicia_ou_continua_viagem():
    resultado = SeparadorViagens().separar([obs(0), obs(5, valida=False), obs(10)])
    assert len(resultado.viagens) == 1
    assert resultado.viagens[0].fim == D0 + timedelta(minutes=10)


def test_limite_da_lacuna_deve_ser_positivo():
    with pytest.raises(ValueError, match="positivo"):
        SeparadorViagens(timedelta(0))
