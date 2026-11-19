"""Rotina interna do acervo de documentos oficiais (US05-T01).

  python -m app.documentos catalogar --pdf ARQ.pdf --titulo T --tipo T \\
      --data-documento AAAA-MM-DD --orgao O --fonte URL --descoberto-em 2026-11-19T10:00-03:00
  python -m app.documentos consultar --id UUID
  python -m app.documentos original --id UUID
"""

import argparse
import json
from datetime import date, datetime
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from app import create_app, db
from app.documentos.service import caminho_original, catalogar_documento, consultar_documento


def _instante(valor):
    instante = datetime.fromisoformat(valor)
    if instante.tzinfo is None:
        raise argparse.ArgumentTypeError("informe o fuso, ex.: 2026-11-19T10:00-03:00")
    return instante


def main(argv=None):
    parser = argparse.ArgumentParser(description="Acervo de documentos oficiais (US05)")
    acoes = parser.add_subparsers(dest="acao", required=True)
    catalogar = acoes.add_parser("catalogar", help="guarda o PDF e cataloga seus metadados")
    catalogar.add_argument("--pdf", required=True, type=Path)
    catalogar.add_argument("--titulo", required=True)
    catalogar.add_argument("--tipo", required=True)
    catalogar.add_argument("--data-documento", required=True, type=date.fromisoformat)
    catalogar.add_argument("--orgao", required=True)
    catalogar.add_argument("--fonte", required=True, help="URL ou referência pública de origem")
    catalogar.add_argument("--descoberto-em", required=True, type=_instante)
    for nome in ("consultar", "original"):
        acoes.add_parser(nome).add_argument("--id", required=True)
    args = parser.parse_args(argv)

    app = create_app()
    with app.app_context():
        try:
            if args.acao == "catalogar":
                documento, criado = catalogar_documento(
                    args.pdf, titulo=args.titulo, tipo=args.tipo,
                    data_documento=args.data_documento, orgao=args.orgao,
                    fonte=args.fonte, descoberto_em=args.descoberto_em,
                )
                resultado = {"estado": "CATALOGADO" if criado else "JA_CATALOGADO",
                             **consultar_documento(documento.id)}
            elif args.acao == "consultar":
                resultado = consultar_documento(args.id)
            else:
                resultado = {"id": args.id, "caminho": str(caminho_original(args.id))}
            print(json.dumps(resultado, ensure_ascii=False, indent=2))
            return 0
        except SQLAlchemyError:
            db.session.rollback()
            print(json.dumps({"estado": "ERRO_BANCO", "motivo": "Verifique conexão e migrações."}))
            return 1
        except (LookupError, ValueError, OSError, RuntimeError) as erro:
            print(json.dumps({"estado": "ERRO", "motivo": str(erro)}, ensure_ascii=False))
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
