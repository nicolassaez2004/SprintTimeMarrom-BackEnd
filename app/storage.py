"""FileStore local de objetos imutáveis, identificados pelo conteúdo."""

import hashlib
import os
from pathlib import Path


def hash_arquivo(caminho):
    digest = hashlib.sha256()
    tamanho = 0
    with open(caminho, "rb") as arquivo:
        for bloco in iter(lambda: arquivo.read(1024 * 1024), b""):
            digest.update(bloco)
            tamanho += len(bloco)
    return digest.hexdigest(), tamanho


class FileStore:
    """Publica ZIPs completos sem sobrescrever ou remover objetos finais.

    O diretório deve estar acessível aos processos que consomem as chaves.
    Em caso de rollback SQL, o objeto é preservado para retomada: removê-lo
    aqui poderia afetar outra captura ainda não confirmada no banco.
    """

    def __init__(self, diretorio):
        self.diretorio = Path(diretorio)
        self.diretorio.mkdir(parents=True, exist_ok=True)

    def publicar_gtfs(self, temporario, digest, tamanho):
        destino = self.diretorio / f"{digest}.zip"
        try:
            # O temporário está no mesmo volume. O link publica atomicamente
            # todos os bytes e falha se o destino já existir.
            os.link(temporario, destino)
        except FileExistsError:
            if hash_arquivo(destino) != (digest, tamanho):
                raise RuntimeError("arquivo de conteúdo GTFS divergente no armazenamento")
        return f"gtfs/{digest}.zip"

    def caminho_gtfs(self, chave, digest):
        """Caminho do ZIP publicado sob `chave`, conferido pelo hash."""
        caminho = self.diretorio / Path(chave).name
        if not caminho.is_file() or hash_arquivo(caminho)[0] != digest:
            raise FileNotFoundError("ZIP GTFS ausente ou divergente do hash")
        return caminho

    def publicar_programacao(self, temporario, digest, tamanho):
        """Publica o JSON bruto sem modificar os bytes recebidos da fonte."""
        destino = self.diretorio / f"{digest}.json"
        try:
            os.link(temporario, destino)
        except FileExistsError:
            if hash_arquivo(destino) != (digest, tamanho):
                raise RuntimeError("arquivo de programação divergente no armazenamento")
        return f"programacao/{digest}.json"

    def caminho_programacao(self, chave, digest):
        caminho = self.diretorio / Path(chave).name
        if not caminho.is_file() or hash_arquivo(caminho)[0] != digest:
            raise FileNotFoundError("JSON de programação ausente ou divergente do hash")
        return caminho
