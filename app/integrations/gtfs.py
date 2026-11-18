"""Cliente HTTP para baixar o feed GTFS sem interpretar ou alterar o ZIP."""

import requests


class GTFSDownloadError(RuntimeError):
    """A fonte não forneceu um download completo utilizável."""


class GTFSClient:
    def __init__(self, timeout=(10, 180)):
        self.timeout = timeout

    def baixar_para(self, url, destino):
        if not url:
            raise GTFSDownloadError("GTFS_SOURCE_URL não configurada")
        try:
            with requests.get(url, stream=True, timeout=self.timeout) as resposta:
                resposta.raise_for_status()
                for bloco in resposta.iter_content(chunk_size=1024 * 1024):
                    if bloco:
                        destino.write(bloco)
        except requests.RequestException as erro:
            # A URL pode conter credenciais temporárias; não a incluímos na mensagem.
            raise GTFSDownloadError("falha ao baixar o feed GTFS") from erro
