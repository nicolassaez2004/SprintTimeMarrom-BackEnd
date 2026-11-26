"""Entrada da atualização mensal usada pelo processo ``app.tarefas``."""
from datetime import datetime
import json
from pathlib import Path

from flask import current_app

from app.contexto.infraestrutura.agenda import atualizar_mensal, registrar_classificacao_vigente


def atualizar_infraestrutura(agora):
    """Importa os snapshots listados no manifesto configurado para o worker.

    O manifesto aponta para arquivos imutáveis baixados da fonte e mantém
    origem/versão/CRS necessários para reproduzir cada captura.
    """
    caminho = current_app.config.get("INFRAESTRUTURA_SNAPSHOT_MANIFEST")
    if not caminho:
        return None
    caminho = Path(caminho)

    def carregar(_agora):
        manifesto = json.loads(caminho.read_text(encoding="utf-8"))
        bases = manifesto.get("bases")
        if not isinstance(bases, list) or not bases:
            raise ValueError("manifesto de infraestrutura precisa listar bases")
        saida = []
        for base in bases:
            entrada = dict(base)
            entrada["origem"] = (caminho.parent / entrada["arquivo"]).resolve()
            del entrada["arquivo"]
            # Data/hora real da captura do arquivo (ISO 8601 com fuso), se registrada.
            if isinstance(entrada.get("capturada_em"), str):
                entrada["capturada_em"] = datetime.fromisoformat(entrada["capturada_em"])
                if entrada["capturada_em"].tzinfo is None:
                    raise ValueError("capturada_em do manifesto precisa de fuso")
            saida.append(entrada)
        return saida

    resultado = atualizar_mensal(agora, carregar)
    return {**resultado, "classificacao": registrar_classificacao_vigente(agora)}
