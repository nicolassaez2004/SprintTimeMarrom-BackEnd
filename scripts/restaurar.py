"""python -m scripts.restaurar --backup DIR --destino-arquivos DIR_NOVO.

Credencial local em RESTORE_DATABASE_URL; nunca passar senha na linha de comando.
"""

import argparse
import json
import os

from dotenv import load_dotenv

from app.operacao.restauracao import restaurar_backup


def main(argv=None):
    load_dotenv()
    parser = argparse.ArgumentParser(description="Restauração somente em NOVA base MySQL local")
    parser.add_argument("--backup", required=True)
    parser.add_argument("--destino-arquivos", required=True)
    args = parser.parse_args(argv)
    try:
        url = os.environ.get("RESTORE_DATABASE_URL")
        if not url:
            raise ValueError("Configure RESTORE_DATABASE_URL local com base marrom_restore_<identificador>")
        resultado = restaurar_backup(args.backup, url, args.destino_arquivos)
        print(json.dumps(resultado, ensure_ascii=False))
        return 0 if resultado["ate_24_horas"] else 1
    except Exception as erro:
        print(json.dumps({"estado": "FALHA", "tipo_erro": type(erro).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
