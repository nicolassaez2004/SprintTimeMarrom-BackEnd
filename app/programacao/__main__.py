"""Comandos explícitos de importação/consulta de programação (US04-T01)."""

import argparse
import json
from datetime import date
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from app import create_app, db
from app.integrations.programacao import CapturaProgramacao
from app.programacao.normalizacao import TIPOS_DIA
from app.programacao.service import consultar_oficial, importar_oficial


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("acao", choices=("importar", "consultar"))
    parser.add_argument("--linha", required=True)
    parser.add_argument("--sentido", required=True, choices=("ida", "volta", "0", "1"))
    parser.add_argument("--data", required=True, type=date.fromisoformat)
    parser.add_argument("--tipo-dia", required=True, choices=tuple(TIPOS_DIA))
    parser.add_argument("--arquivo-json", type=Path, help="captura oficial obtida externamente")
    parser.add_argument("--fonte-url", help="URL pública SPTrans da captura manual")
    parser.add_argument("--versao-fonte", help="hash histórico, somente para consulta")
    parser.add_argument("--versao-gtfs", help="contexto GTFS explícito quando há vigências sobrepostas")
    args = parser.parse_args(argv)
    if bool(args.arquivo_json) != bool(args.fonte_url):
        parser.error("--arquivo-json e --fonte-url devem ser informados juntos")
    if args.acao == "consultar" and args.arquivo_json:
        parser.error("consulta não importa arquivo")
    if args.acao == "importar" and args.versao_fonte:
        parser.error("--versao-fonte é usado apenas na consulta")
    app = create_app()
    with app.app_context():
        try:
            argumentos = (args.linha, args.sentido, args.data, args.tipo_dia)
            if args.acao == "consultar":
                resultado = consultar_oficial(*argumentos, versao_fonte=args.versao_fonte,
                                               versao_gtfs_id=args.versao_gtfs)
            else:
                captura = (CapturaProgramacao(args.arquivo_json.read_bytes(), args.fonte_url)
                           if args.arquivo_json else None)
                resultado = importar_oficial(*argumentos, captura=captura, versao_gtfs_id=args.versao_gtfs)
            print(json.dumps(resultado, ensure_ascii=False, indent=2))
            return 2 if resultado["estado"] in ("FALLBACK_GTFS_NECESSARIO", "NAO_IMPORTADA") else 0
        except SQLAlchemyError:
            db.session.rollback()
            print(json.dumps({"estado": "ERRO_BANCO", "motivo": "Verifique conexão e migrações com o responsável."}))
            return 1
        except (LookupError, ValueError, OSError) as erro:
            # Mensagens destas validações não contêm URLs de conexão/senhas.
            print(json.dumps({"estado": "ERRO", "motivo": str(erro)}, ensure_ascii=False))
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
