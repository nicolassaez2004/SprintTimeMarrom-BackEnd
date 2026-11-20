"""python -m scripts.backup --destino DIRETORIO_FORA_DO_REPOSITORIO"""

import argparse
import json

from app import create_app, db
from app.operacao.backup import criar_backup


def main(argv=None):
    parser = argparse.ArgumentParser(description="Backup consistente do MySQL e originais")
    parser.add_argument("--destino", required=True)
    args = parser.parse_args(argv)
    app = create_app()
    with app.app_context():
        try:
            resultado = criar_backup(args.destino)
            print(json.dumps({k: resultado[k] for k in ("estado", "diretorio", "tabelas", "originais")}, ensure_ascii=False))
            return 0
        except Exception as erro:
            print(json.dumps({"estado": "FALHA", "tipo_erro": type(erro).__name__}))
            return 1
        finally:
            db.session.remove()
            db.engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
