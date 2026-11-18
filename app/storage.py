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
