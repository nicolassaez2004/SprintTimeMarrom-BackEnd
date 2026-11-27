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

    def _publicar(self, temporario, digest, tamanho, extensao, prefixo, descricao):
        """Publica os bytes sem modificá-los; conteúdo igual já publicado é aceito."""
        destino = self.diretorio / f"{digest}.{extensao}"
        try:
            # O temporário está no mesmo volume. O link publica atomicamente
            # todos os bytes e falha se o destino já existir.
            os.link(temporario, destino)
        except FileExistsError:
            if hash_arquivo(destino) != (digest, tamanho):
                raise RuntimeError(f"{descricao} divergente no armazenamento")
        return f"{prefixo}/{digest}.{extensao}"

    def _caminho(self, chave, digest, descricao):
        """Caminho do objeto publicado sob `chave`, conferido pelo hash."""
        caminho = self.diretorio / Path(chave).name
        if not caminho.is_file() or hash_arquivo(caminho)[0] != digest:
            raise FileNotFoundError(f"{descricao} ausente ou divergente do hash")
        return caminho

    def publicar_gtfs(self, temporario, digest, tamanho):
        return self._publicar(temporario, digest, tamanho, "zip", "gtfs",
                              "arquivo de conteúdo GTFS")

    def caminho_gtfs(self, chave, digest):
        return self._caminho(chave, digest, "ZIP GTFS")

    def publicar_programacao(self, temporario, digest, tamanho):
        return self._publicar(temporario, digest, tamanho, "json", "programacao",
                              "arquivo de programação")

    def caminho_programacao(self, chave, digest):
        return self._caminho(chave, digest, "JSON de programação")

    def publicar_pdf(self, temporario, digest, tamanho):
        return self._publicar(temporario, digest, tamanho, "pdf", "documentos", "PDF")

    def caminho_pdf(self, chave, digest):
        return self._caminho(chave, digest, "PDF original")

    def publicar_extracao(self, temporario, digest, tamanho, extensao):
        return self._publicar(temporario, digest, tamanho, extensao, "extracoes",
                              "arquivo de extração")

    def caminho_extracao(self, chave, digest):
        return self._caminho(chave, digest, "arquivo de extração")
