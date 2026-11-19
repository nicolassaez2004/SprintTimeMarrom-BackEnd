"""Consulta pública utilizada pelo site SPTrans; não é a API de previsões."""

import re
from dataclasses import dataclass
from urllib.parse import urlparse

import requests

BASE_OFICIAL = "https://itinerariosapi.sptrans.com.br/"


class FonteOficialIndisponivel(RuntimeError):
    def __init__(self, motivo):
        self.motivo = motivo
        super().__init__(motivo)


class ProgramacaoOficialInvalida(ValueError):
    pass


def normalizar_linha(linha):
    codigo = str(linha).strip().upper()
    if not re.fullmatch(r"[0-9A-Z]{4}-[0-9]{2}", codigo):
        raise ValueError("linha deve ter o código completo, por exemplo 3762-10")
    return codigo


def validar_url_oficial(url):
    if not isinstance(url, str) or len(url) > 512:
        raise ProgramacaoOficialInvalida("URL de procedência ausente ou muito longa")
    try:
        parsed = urlparse(url)
        porta = parsed.port
    except ValueError as erro:
        raise ProgramacaoOficialInvalida("URL de procedência inválida") from erro
    if (parsed.scheme != "https" or parsed.hostname not in
            {"itinerariosapi.sptrans.com.br", "www.sptrans.com.br", "sptrans.com.br"}
            or parsed.username or parsed.password or parsed.fragment or parsed.query
            or porta not in (None, 443)):
        raise ProgramacaoOficialInvalida("a procedência deve ser uma URL HTTPS pública da SPTrans")


@dataclass(frozen=True)
class CapturaProgramacao:
    conteudo: bytes
    fonte_url: str


class SPTransProgrammingClient:
    """Endpoints observados nos scripts públicos da consulta Por Linha.

    POST é o método de consulta do portal. Nenhuma configuração externa ou
    dado do projeto é enviado, e 403 não é contornado com credenciais.
    """

    def __init__(self, session=None, timeout=(10, 45)):
        self.session = session or requests.Session()
        self.timeout = timeout

    def _post(self, endpoint, payload=None):
        try:
            resposta = self.session.post(BASE_OFICIAL + endpoint, json=payload,
                                        timeout=self.timeout, allow_redirects=False,
                                        headers={"Content-Type": "application/json", "Accept": "application/json"})
            if resposta.status_code != 200:
                raise FonteOficialIndisponivel(f"HTTP_{resposta.status_code}")
            return resposta
        except requests.RequestException as erro:
            raise FonteOficialIndisponivel("FALHA_DE_REDE") from erro

    def obter(self, linha):
        codigo = normalizar_linha(linha)
        try:
            catalogo = self._post("RetornarLinhasTeste").json()
        except ValueError as erro:
            raise FonteOficialIndisponivel("CATALOGO_NAO_JSON") from erro
        if not isinstance(catalogo, list):
            raise FonteOficialIndisponivel("CATALOGO_INVALIDO")
        candidatas = [item for item in catalogo if isinstance(item, dict)
                      and str(item.get("codigo", "")).strip().upper() == codigo]
        if not candidatas:
            raise FonteOficialIndisponivel("LINHA_NAO_PUBLICADA")
        if len(candidatas) != 1 or not candidatas[0].get("CdPjOID"):
            raise FonteOficialIndisponivel("LINHA_AMBIGUA_OU_SEM_IDENTIFICADOR")
        resposta = self._post("BuscarDetalheLinhaTeste",
                              {"codPlanejamento": candidatas[0]["CdPjOID"]})
        return CapturaProgramacao(bytes(resposta.content), BASE_OFICIAL + "BuscarDetalheLinhaTeste")
