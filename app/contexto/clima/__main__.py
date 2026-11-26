"""Operação manual do clima por trecho (US06). Saída em JSON.

    python -m app.contexto.clima estacoes
    python -m app.contexto.clima coletar [--fonte INMET|CGE]
    python -m app.contexto.clima associar --versao-gtfs <uuid> [--raio-m 25000]
    python -m app.contexto.clima cobertura --mes 2026-11 [--fonte INMET]

A coleta contínua roda no processo de tarefas (`python -m app.tarefas`).
"""

import argparse
import json
import re
import sys

from app import create_app
from app.contexto.clima.associacao import associar_trechos_a_estacoes
from app.contexto.clima.coleta import coletar_cge, coletar_inmet, sincronizar_estacoes_inmet
from app.consultas.clima import cobertura_mensal
from app.integrations.clima import FonteClimaIndisponivel


def _mes(valor):
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", valor):
        raise argparse.ArgumentTypeError("use AAAA-MM")
    ano, mes = valor.split("-")
    return int(ano), int(mes)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    comandos = parser.add_subparsers(dest="comando", required=True)
    comandos.add_parser("estacoes", help="Atualiza o catálogo de estações INMET próximas")
    coletar = comandos.add_parser("coletar", help="Executa uma coleta agora")
    coletar.add_argument("--fonte", choices=("INMET", "CGE"), default="INMET")
    associar = comandos.add_parser("associar", help="Vincula os trechos de uma versão GTFS")
    associar.add_argument("--versao-gtfs", required=True)
    associar.add_argument("--fonte", choices=("INMET", "CGE"), default="INMET")
    associar.add_argument("--raio-m", type=float)
    cobertura = comandos.add_parser("cobertura", help="Cobertura mensal x meta de 95%%")
    cobertura.add_argument("--mes", type=_mes, required=True)
    cobertura.add_argument("--fonte", choices=("INMET", "CGE"), default="INMET")
    args = parser.parse_args(argv)

    app = create_app()
    with app.app_context():
        try:
            if args.comando == "estacoes":
                saida = {"estacoes_gravadas": sincronizar_estacoes_inmet()}
            elif args.comando == "coletar":
                coleta = (coletar_inmet if args.fonte == "INMET" else coletar_cge)()
                saida = {"fonte": coleta.fonte, "estado": coleta.estado, "recebidas": coleta.recebidas,
                         "novas": coleta.novas, "erro": coleta.erro}
            elif args.comando == "associar":
                saida = associar_trechos_a_estacoes(
                    args.versao_gtfs, fonte=args.fonte, raio_maximo_m=args.raio_m).como_dict()
            else:
                saida = cobertura_mensal(*args.mes, fonte=args.fonte)
        except FonteClimaIndisponivel as erro:
            print(json.dumps({"erro": str(erro)}, ensure_ascii=False), file=sys.stderr)
            return 1
    print(json.dumps(saida, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
