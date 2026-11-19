"""Contrato observado no JavaScript público, com respostas SINTÉTICAS."""

from datetime import date, datetime, timezone
from unittest.mock import Mock

import pytest
import requests

from app.integrations.programacao import (
    BASE_OFICIAL, CapturaProgramacao, FonteOficialIndisponivel,
    ProgramacaoOficialInvalida, SPTransProgrammingClient,
)
from app.programacao.normalizacao import normalizar

DATA = date(2026, 11, 19)


def test_cliente_usa_catalogo_exato_e_identificador_publico_sem_token(fabrica_captura):
    sessao = Mock()
    catalogo = Mock(status_code=200)
    catalogo.json.return_value = [{"codigo": "3762-21", "CdPjOID": "errado"},
                                 {"codigo": "3762-10", "CdPjOID": "oficial-sintetico"}]
    detalhe = Mock(status_code=200, content=fabrica_captura().conteudo)
    sessao.post.side_effect = [catalogo, detalhe]
    captura = SPTransProgrammingClient(sessao).obter(" 3762-10 ")
    assert captura.fonte_url == BASE_OFICIAL + "BuscarDetalheLinhaTeste"
    assert captura.conteudo == detalhe.content
    primeira, segunda = sessao.post.call_args_list
    assert primeira.args == (BASE_OFICIAL + "RetornarLinhasTeste",)
    assert segunda.kwargs["json"] == {"codPlanejamento": "oficial-sintetico"}
    assert segunda.kwargs["allow_redirects"] is False
    assert segunda.kwargs["timeout"] == (10, 45)
    assert "Authorization" not in segunda.kwargs["headers"]


@pytest.mark.parametrize("status", [403, 404, 500, 302])
def test_erro_http_nao_e_tentado_novamente_nem_contornado(status):
    sessao = Mock()
    sessao.post.return_value = Mock(status_code=status)
    with pytest.raises(FonteOficialIndisponivel, match=f"HTTP_{status}"):
        SPTransProgrammingClient(sessao).obter("3762-10")
    assert sessao.post.call_count == 1


def test_timeout_e_catalogo_sem_correspondencia_nao_sao_ausencia_de_partidas():
    sessao = Mock()
    sessao.post.side_effect = requests.Timeout()
    with pytest.raises(FonteOficialIndisponivel, match="FALHA_DE_REDE"):
        SPTransProgrammingClient(sessao).obter("3762-10")
    sessao.post.side_effect = None
    sessao.post.return_value = Mock(status_code=200)
    sessao.post.return_value.json.return_value = [{"codigo": "3762-21", "CdPjOID": "outro"}]
    with pytest.raises(FonteOficialIndisponivel, match="LINHA_NAO_PUBLICADA"):
        SPTransProgrammingClient(sessao).obter("3762-10")
    sessao.post.return_value.json.return_value = [{"codigo": "3762-10", "CdPjOID": "a"},
                                                {"codigo": "3762-10", "CdPjOID": "b"}]
    with pytest.raises(FonteOficialIndisponivel, match="AMBIGUA"):
        SPTransProgrammingClient(sessao).obter("3762-10")


@pytest.mark.parametrize("horarios,esperado", [
    (("23:59:59", "24:10:00", "25:00"),
     ("2026-11-20T02:59:59+00:00", "2026-11-20T03:10:00+00:00", "2026-11-20T04:00:00+00:00")),
    (("06:00", "06:00"), ("2026-11-19T09:00:00+00:00", "2026-11-19T09:00:00+00:00")),
])
def test_horarios_acima_de_24h_e_simultaneos_sao_preservados(fabrica_captura, horarios, esperado):
    resultado = normalizar(fabrica_captura(horarios), "3762-10", "ida", DATA, "UTIL")
    assert tuple(saida.isoformat() for saida in resultado.saidas) == esperado


@pytest.mark.parametrize("horarios", [("12:61",), ("06:00:99",), ("12:00", "11:59"),
                                      ("25:00", "01:10"), ("23:00", "01:00", "25:00"),
                                      (None,), ("5:00",)])
def test_horarios_invalidos_ou_ambiguos_sao_rejeitados(fabrica_captura, horarios):
    with pytest.raises(ProgramacaoOficialInvalida):
        normalizar(fabrica_captura(horarios), "3762-10", "ida", DATA, "UTIL")


def test_tipo_de_dia_e_explicito_e_feriado_sem_grupo_exige_fallback(fabrica_captura):
    # Uma data que cai em quinta pode usar outra grade por decisão operacional.
    resultado = normalizar(fabrica_captura(("07:00",), tipo=1), "3762-10", "ida", DATA, "SABADO")
    assert resultado.saidas[0].hour == 10
    with pytest.raises(FonteOficialIndisponivel, match="FERIADO_NAO_PUBLICADO"):
        normalizar(fabrica_captura(), "3762-10", "ida", DATA, "FERIADO")
    assert normalizar(fabrica_captura((), tipo=3), "3762-10", "ida", DATA, "FERIADO").saidas == ()


def test_contexto_nao_aceita_data_inferida_de_instante_nem_sentido_olho_vivo(fabrica_captura):
    with pytest.raises(TypeError, match="date"):
        normalizar(fabrica_captura(), "3762-10", "ida", datetime.now(timezone.utc), "UTIL")
    with pytest.raises(ValueError, match="sentido"):
        normalizar(fabrica_captura(), "3762-10", "2", DATA, "UTIL")
    with pytest.raises(ValueError, match="tipo_dia"):
        normalizar(fabrica_captura(), "3762-10", "ida", DATA, "AUTOMATICO")


@pytest.mark.parametrize("bruto", [b"<html>erro</html>", b"null", b"\xff"])
def test_html_json_incompleto_e_codificacao_invalida_nao_viram_agenda_vazia(bruto):
    with pytest.raises(ProgramacaoOficialInvalida):
        normalizar(CapturaProgramacao(bruto, BASE_OFICIAL), "3762-10", "ida", DATA, "UTIL")
