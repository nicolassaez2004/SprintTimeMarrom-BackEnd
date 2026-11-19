"""Uso: python -m app.exportacoes.parquet_cli --data AAAA-MM-DD --saida DIR"""

import argparse
from datetime import date
from pathlib import Path

from app import create_app
from app.exportacoes.parquet import gerar_parquet_diario


def main():
    parser = argparse.ArgumentParser(description="Exporta brutos GPS diários para Parquet")
    parser.add_argument("--data", required=True, type=date.fromisoformat, help="dia UTC (AAAA-MM-DD)")
    parser.add_argument("--saida", default="exportacoes", help="diretório de publicação")
    parser.add_argument("--lote", type=int, default=5000, help="linhas por lote")
    args = parser.parse_args()
    destino = Path(args.saida) / f"registros_gps_{args.data.isoformat()}.parquet"
    app = create_app()
    with app.app_context():
        resultado = gerar_parquet_diario(args.data, destino, tamanho_lote=args.lote)
    print(f"{resultado['registros']} registros publicados em {resultado['arquivo']}")


if __name__ == "__main__":
    main()
