"""Classifica uma rede GTFS usando os snapshots vigentes na data informada."""
import argparse
from datetime import date
import json

from app import create_app
from app.contexto.infraestrutura.associacao import classificar_trechos


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--versao-gtfs", required=True,
                        help="ID explícito da versão GTFS cujos trechos serão classificados")
    parser.add_argument("--data", required=True, type=date.fromisoformat,
                        help="data de vigência dos snapshots, formato AAAA-MM-DD")
    args = parser.parse_args(argv)
    app = create_app()
    with app.app_context():
        resultado = classificar_trechos(args.versao_gtfs, quando=args.data)
        print(json.dumps(resultado, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
