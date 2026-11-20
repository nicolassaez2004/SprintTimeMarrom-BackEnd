"""Publicação durável de evidências sem anunciar arquivos parciais."""

import json
import os
import tempfile
from pathlib import Path


def gravar_json(caminho, dados):
    caminho = Path(caminho)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    fd, temporario = tempfile.mkstemp(dir=caminho.parent, suffix=".partial")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(dados, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporario, caminho)
    finally:
        Path(temporario).unlink(missing_ok=True)


def caminho_interno(raiz, relativo):
    raiz = Path(raiz).resolve()
    caminho = (raiz / relativo).resolve()
    if caminho == raiz or not caminho.is_relative_to(raiz):
        raise ValueError("Caminho fora do conjunto de recuperação")
    return caminho
