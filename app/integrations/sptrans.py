"""Cliente da API Olho Vivo (SPTrans).

Só faz autenticação, consulta e validação mínima do formato. A resposta é
devolvida como a API a enviou (JSON decodificado, com os horários `hr` e `ta`
intactos); tratar ou descartar dados é responsabilidade das camadas acima.

Nenhuma mensagem de erro inclui o token, cookies ou a URL de autenticação.
"""

import requests

from flask import current_app


TIMEOUT_SEGUNDOS = 15


class SPTransError(RuntimeError):
    """Base de todas as falhas do cliente SPTrans."""


class SPTransConexaoError(SPTransError):
    """Falha de rede: timeout, DNS, conexão recusada etc."""


class SPTransAutenticacaoError(SPTransError):
    """A SPTrans recusou o token (`/Login/Autenticar` não devolveu `true`)."""


class SPTransHTTPError(SPTransError):
    """A SPTrans respondeu com status HTTP de erro."""

    def __init__(self, caminho, status_code):
        self.caminho = caminho
        self.status_code = status_code
        super().__init__(f"SPTrans respondeu HTTP {status_code} em {caminho}")


class SPTransRespostaInvalidaError(SPTransError):
    """A resposta não é JSON ou não tem o formato documentado."""


class SPTransClient:
    def __init__(self, session=None):
        config = current_app.config

        self.base_url = config["SPTRANS_API_URL"].rstrip("/")
        self.token = config["SPTRANS_TOKEN"]

        if not self.token:
            raise ValueError("SPTRANS_TOKEN nao configurado no .env")

        # A sessão guarda o cookie de autenticação e é reutilizada nas consultas.
        self.session = session or requests.Session()

        self.autenticar()

    def __repr__(self):
        # Nunca exibe o token.
        return f"<SPTransClient base_url={self.base_url!r}>"

    def _requisitar(self, metodo, caminho, params=None):
        try:
            response = self.session.request(
                metodo,
                f"{self.base_url}{caminho}",
                params=params,
                timeout=TIMEOUT_SEGUNDOS,
            )
        except requests.RequestException as error:
            # A exceção original contém a URL (com o token, na autenticação).
            raise SPTransConexaoError(
                f"Falha de comunicação com a SPTrans em {caminho} "
                f"({type(error).__name__})"
            ) from None

        # Sem raise_for_status(): a mensagem dele inclui a URL completa.
        if response.status_code >= 400:
            raise SPTransHTTPError(caminho, response.status_code)

        try:
            return response.json()
        except ValueError:
            raise SPTransRespostaInvalidaError(
                f"Resposta da SPTrans em {caminho} não é JSON válido"
            ) from None

    def _consultar(self, caminho, params=None):
        """GET autenticado; se a sessão expirou (401), autentica de novo uma vez."""
        try:
            return self._requisitar("GET", caminho, params)
        except SPTransHTTPError as error:
            if error.status_code != 401:
                raise
        self.autenticar()
        return self._requisitar("GET", caminho, params)

    def autenticar(self):
        """Autentica na API Olho Vivo (`POST /Login/Autenticar`)."""
        resultado = self._requisitar(
            "POST", "/Login/Autenticar", params={"token": self.token}
        )
        if resultado is not True:
            raise SPTransAutenticacaoError("Falha na autenticacao com a SPTrans")

    def buscar_linhas(self, termo):
        """Busca linhas pelo numero ou nome (`GET /Linha/Buscar`). Lista vazia é válida."""
        resposta = self._consultar("/Linha/Buscar", {"termosBusca": termo})
        if not isinstance(resposta, list):
            raise SPTransRespostaInvalidaError(
                "Resposta de /Linha/Buscar não é uma lista"
            )
        return resposta

    def buscar_posicoes(self, codigo_linha):
        """Busca as posicoes de uma linha (`GET /Posicao/Linha`).

        Formato: `{"hr": "HH:MM", "vs": [...]}`; `vs` vazio é consulta válida.
        """
        resposta = self._consultar("/Posicao/Linha", {"codigoLinha": codigo_linha})
        self._validar_posicoes(resposta, "/Posicao/Linha", "vs")
        return resposta

    def buscar_todas_posicoes(self):
        """Busca a última posição publicada de toda a frota (`GET /Posicao`).

        Formato: `{"hr": "HH:MM", "l": [{"c", "cl", "sl", ..., "vs": [...]}]}`.
        `l` vazio é uma consulta válida sem veículos, não um erro.
        """
        resposta = self._consultar("/Posicao")
        self._validar_posicoes(resposta, "/Posicao", "l")
        return resposta

    @staticmethod
    def _validar_posicoes(resposta, caminho, chave_lista):
        # Só confere a estrutura; nenhum campo é alterado ou descartado.
        if (
            not isinstance(resposta, dict)
            or "hr" not in resposta
            or not isinstance(resposta.get(chave_lista), list)
        ):
            raise SPTransRespostaInvalidaError(
                f"Resposta de {caminho} sem 'hr' ou lista '{chave_lista}'"
            )

    @staticmethod
    def contar_veiculos(resposta_frota):
        """Total de veículos em uma resposta de `buscar_todas_posicoes`."""
        return sum(len(linha.get("vs") or []) for linha in resposta_frota["l"])
