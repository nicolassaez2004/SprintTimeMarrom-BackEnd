import requests

from flask import current_app


class SPTransClient:
    def __init__(self):
        # Configuracoes do Flask
        config = current_app.config

        self.base_url = config["SPTRANS_API_URL"]
        self.token = config["SPTRANS_TOKEN"]

        if not self.token:
            raise ValueError(
                "SPTRANS_TOKEN nao configurado no .env"
            )
        
        # Mantem os cookies da autenticacao
        self.session = requests.Session()

        self.autenticar()

    def autenticar(self):
        """Autentica na API Olho Vivo."""

        try:
            response = self.session.post(
                f"{self.base_url}/Login/Autenticar",
                params={"token": self.token},
                timeout=15
            )
            response.raise_for_status()
        except requests.RequestException as error:
            # A URL da autenticação contém o token; não propague detalhes dela.
            raise RuntimeError(
                f"Falha de comunicação com a SPTrans ({type(error).__name__})"
            ) from None

        if response.json() is not True:
            raise RuntimeError(
                "Falha na autenticacao com a SPTrans"
            )

    def buscar_linhas(self, termo):
        """Busca linhas pelo numero ou nome."""

        response = self.session.get(
            f"{self.base_url}/Linha/Buscar",
            params={"termosBusca": termo},
            timeout=15
        )

        response.raise_for_status()

        return response.json()

    def buscar_posicoes(self, codigo_linha):
        """Busca as posicoes de uma linha."""

        response = self.session.get(
            f"{self.base_url}/Posicao/Linha",
            params={"codigoLinha": codigo_linha},
            timeout=15
        )

        response.raise_for_status()

        return response.json()

    def buscar_todas_posicoes(self):
        """Busca a última posição publicada para todos os veículos."""
        response = self.session.get(
            f"{self.base_url}/Posicao",
            timeout=15
        )

        response.raise_for_status()
        return response.json()
