"""Publicação/verificação interna da US09, com saída diferente de zero em falha."""

import argparse
import json
from datetime import date, datetime, timezone

from app import create_app, db
from app.exportacoes.conferencia import conferir_parquet
from app.exportacoes.parquet import (
    dia_a_publicar,
    diretorio_exportacoes,
    nome_arquivo,
    publicar_brutos,
)
from app.operacao.arquivos import gravar_json


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=date.fromisoformat)
    parser.add_argument("--publicar", action="store_true")
    parser.add_argument("--evidencia")
    args = parser.parse_args(argv)
    app = create_app()
    with app.app_context():
        try:
            agora = datetime.now(timezone.utc)
            if args.publicar:
                publicar_brutos(agora)
            dia = args.data or dia_a_publicar(agora)
            resultado = conferir_parquet(dia, diretorio_exportacoes() / nome_arquivo(dia))
            saida = args.evidencia or diretorio_exportacoes() / f"conferencia-{dia}.json"
            gravar_json(saida, resultado)
            print(json.dumps(resultado, ensure_ascii=False))
            return 0 if resultado["aprovado"] else 1
        except Exception as erro:
            print(json.dumps({"estado": "FALHA", "tipo_erro": type(erro).__name__}))
            return 1
        finally:
            db.session.remove()
            db.engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
