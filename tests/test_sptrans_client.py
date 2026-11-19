"""Testes do SPTransClient com respostas simuladas (nenhuma chamada real).

O teste real, ao final, só roda com SPTRANS_TESTE_REAL=1 e token no .env.
"""

import copy
import os
from unittest.mock import Mock

import pytest
import requests

from app import create_app
from app.integrations.sptrans import (
    SPTransAutenticacaoError,
    SPTransClient,
    SPTransConexaoError,
    SPTransConfiguracaoError,
    SPTransError,
    SPTransHTTPError,
    SPTransRespostaInvalidaError,
)


TOKEN_FALSO = "token-falso-de-teste"
BASE_URL = "https://olhovivo.exemplo/v2.1"

# Formato documentado de GET /Posicao, com valores sanitizados.
RESPOSTA_FROTA = {
    "hr": "15:00",
    "l": [
        {
            "c": "8000-10", "cl": 1234, "sl": 1, "lt0": "TERM. A", "lt1": "TERM. B",
            "qv": 2,
            "vs": [
                {"p": "11111", "a": True, "ta": "2026-11-18T18:00:05Z",
                 "py": -23.55, "px": -46.63},
                {"p": "22222", "a": False, "ta": "2026-11-18T17:59:50Z",
                 "py": -23.56, "px": -46.64},
            ],
        },
    ],
}
RESPOSTA_FROTA_VAZIA = {"hr": "03:10", "l": []}


def resposta(status=200, json=None, json_invalido=False):
    r = Mock(status_code=status)
    if json_invalido:
        r.json.side_effect = requests.exceptions.JSONDecodeError("x", "<html>", 0)
    else:
        r.json.return_value = json
    return r


@pytest.fixture
def app():
    app = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SPTRANS_API_URL": BASE_URL,
        "SPTRANS_TOKEN": TOKEN_FALSO,
    })
    with app.app_context():
        yield app


def criar_cliente(*respostas):
    sessao = Mock()
    sessao.request.side_effect = list(respostas)
    return SPTransClient(session=sessao), sessao


def test_autentica_com_post_e_reutiliza_sessao(app):
    cliente, sessao = criar_cliente(resposta(json=True), resposta(json=RESPOSTA_FROTA))

    cliente.buscar_todas_posicoes()

    login, consulta = sessao.request.call_args_list
    assert login.args == ("POST", f"{BASE_URL}/Login/Autenticar")
    assert login.kwargs["params"] == {"token": TOKEN_FALSO}
    assert consulta.args == ("GET", f"{BASE_URL}/Posicao")
    assert "token" not in (consulta.kwargs["params"] or {})
    assert all(c.kwargs["timeout"] for c in sessao.request.call_args_list)


def test_posicao_devolve_resposta_original_com_horarios(app):
    original = copy.deepcopy(RESPOSTA_FROTA)
    cliente, _ = criar_cliente(resposta(json=True), resposta(json=RESPOSTA_FROTA))

    resultado = cliente.buscar_todas_posicoes()

    assert resultado == original
    assert resultado["hr"] == "15:00"
    assert resultado["l"][0]["vs"][0]["ta"] == "2026-11-18T18:00:05Z"
    assert SPTransClient.contar_veiculos(resultado) == 2


def test_consulta_valida_sem_veiculos_nao_e_erro(app):
    cliente, _ = criar_cliente(resposta(json=True), resposta(json=RESPOSTA_FROTA_VAZIA))

    resultado = cliente.buscar_todas_posicoes()

    assert resultado == RESPOSTA_FROTA_VAZIA
    assert SPTransClient.contar_veiculos(resultado) == 0


def test_token_recusado_e_falha_de_autenticacao(app):
    with pytest.raises(SPTransAutenticacaoError) as erro:
        criar_cliente(resposta(json=False))
    assert TOKEN_FALSO not in str(erro.value)


def test_token_ausente_nao_chama_a_api(app):
    app.config["SPTRANS_TOKEN"] = ""
    sessao = Mock()
    with pytest.raises(ValueError):
        SPTransClient(session=sessao)
    sessao.request.assert_not_called()


@pytest.mark.parametrize("status", [400, 500, 503])
def test_erro_http_na_consulta(app, status):
    cliente, _ = criar_cliente(resposta(json=True), resposta(status=status))

    with pytest.raises(SPTransHTTPError) as erro:
        cliente.buscar_todas_posicoes()

    assert erro.value.status_code == status
    assert not isinstance(erro.value, SPTransAutenticacaoError)


def test_erro_http_na_autenticacao_nao_expoe_url(app):
    with pytest.raises(SPTransHTTPError) as erro:
        criar_cliente(resposta(status=500))
    assert TOKEN_FALSO not in str(erro.value)
    assert BASE_URL not in str(erro.value)


def test_sessao_expirada_reautentica_uma_vez(app):
    cliente, sessao = criar_cliente(
        resposta(json=True),
        resposta(status=401),
        resposta(json=True),
        resposta(json=RESPOSTA_FROTA),
    )

    assert cliente.buscar_todas_posicoes() == RESPOSTA_FROTA
    metodos = [c.args[0] for c in sessao.request.call_args_list]
    assert metodos == ["POST", "GET", "POST", "GET"]


def test_401_persistente_vira_erro_http(app):
    cliente, _ = criar_cliente(
        resposta(json=True), resposta(status=401), resposta(json=True), resposta(status=401)
    )
    with pytest.raises(SPTransHTTPError) as erro:
        cliente.buscar_todas_posicoes()
    assert erro.value.status_code == 401


def test_falha_de_rede_nao_expoe_token(app):
    sessao = Mock()
    sessao.request.side_effect = requests.ConnectionError(
        f"{BASE_URL}/Login/Autenticar?token={TOKEN_FALSO}"
    )
    with pytest.raises(SPTransConexaoError) as erro:
        SPTransClient(session=sessao)
    assert TOKEN_FALSO not in str(erro.value)
    assert erro.value.__cause__ is None
    assert erro.value.__suppress_context__


@pytest.mark.parametrize("corpo", [
    {"json_invalido": True},
    {"json": None},
    {"json": []},
    {"json": {"l": []}},
    {"json": {"hr": "15:00"}},
    {"json": {"hr": "15:00", "l": None}},
])
def test_resposta_invalida(app, corpo):
    cliente, _ = criar_cliente(resposta(json=True), resposta(**corpo))
    with pytest.raises(SPTransRespostaInvalidaError):
        cliente.buscar_todas_posicoes()


def test_posicao_por_linha_aceita_vs_vazio_e_recusa_formato_errado(app):
    cliente, _ = criar_cliente(
        resposta(json=True),
        resposta(json={"hr": "15:00", "vs": []}),
        resposta(json={"hr": "15:00"}),
    )
    assert cliente.buscar_posicoes(1234) == {"hr": "15:00", "vs": []}
    with pytest.raises(SPTransRespostaInvalidaError):
        cliente.buscar_posicoes(1234)


def test_todas_as_falhas_derivam_de_sptrans_error():
    for classe in (SPTransConexaoError, SPTransAutenticacaoError,
                   SPTransHTTPError, SPTransRespostaInvalidaError):
        assert issubclass(classe, SPTransError)
        assert issubclass(classe, RuntimeError)


def test_repr_nao_mostra_token(app):
    cliente, _ = criar_cliente(resposta(json=True))
    assert TOKEN_FALSO not in repr(cliente)


@pytest.mark.parametrize('erro',[requests.exceptions.SSLError('certificado'),requests.exceptions.InvalidURL('url')])
def test_tls_e_url_invalidos_sao_permanentes_e_nao_expoem_token(app,erro):
    sessao=Mock();sessao.request.side_effect=erro
    with pytest.raises(SPTransConfiguracaoError) as capturada:SPTransClient(session=sessao)
    assert TOKEN_FALSO not in str(capturada.value)


def test_cliente_fecha_recursos_da_sessao(app):
    cliente,sessao=criar_cliente(resposta(json=True))
    cliente.fechar();sessao.close.assert_called_once()


@pytest.mark.skipif(
    os.getenv("SPTRANS_TESTE_REAL") != "1",
    reason="teste manual: defina SPTRANS_TESTE_REAL=1 e SPTRANS_TOKEN no .env",
)
def test_real_posicao_da_frota():
    app = create_app({"SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    with app.app_context():
        resultado = SPTransClient().buscar_todas_posicoes()
    print(f"hr={resultado['hr']} linhas={len(resultado['l'])} "
          f"veiculos={SPTransClient.contar_veiculos(resultado)}")
