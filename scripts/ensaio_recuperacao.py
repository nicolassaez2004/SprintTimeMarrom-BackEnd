"""Ensaio semestral: restaura o último backup em uma base e diretório NOVOS."""

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv
from sqlalchemy.engine import make_url

from app.operacao.backup import validar_conjunto
from app.operacao.restauracao import restaurar_backup, validar_alvo


def main(argv=None):
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--backups", required=True)
    parser.add_argument("--destino", required=True)
    args = parser.parse_args(argv)
    try:
        candidatos = []
        for pasta in Path(args.backups).glob("backup-*"):
            if pasta.is_dir():
                manifesto, _ = validar_conjunto(pasta)
                candidatos.append((manifesto["concluida_em"], pasta))
        if not candidatos:
            raise ValueError("Nenhum backup completo disponível")
        url = validar_alvo(make_url(os.environ["RESTORE_DATABASE_URL"]))
        identificador = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_") + uuid4().hex[:6]
        url = url.set(database="marrom_restore_" + identificador)
        resultado = restaurar_backup(max(candidatos)[1], url, Path(args.destino) / identificador)
        print(json.dumps(resultado, ensure_ascii=False))
        return 0 if resultado["ate_24_horas"] else 1
    except Exception as erro:
        print(json.dumps({"estado": "FALHA", "tipo_erro": type(erro).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
