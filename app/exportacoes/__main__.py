"""Uso: python -m app.exportacoes --data AAAA-MM-DD [--saida DIR]

Sem --saida, usa EXPORTACOES_DIR (ou instance/exportacoes), o mesmo diretório
publicado por GET /downloads/brutos e pela tarefa agendada.
"""

import argparse
from datetime import date
from pathlib import Path

from app import create_app
from app.exportacoes.parquet import diretorio_exportacoes, gerar_parquet_diario, nome_arquivo


def main():
    parser = argparse.ArgumentParser(description="Exporta brutos GPS diários para Parquet")
    parser.add_argument("--data", required=True, type=date.fromisoformat, help="dia em America/Sao_Paulo (AAAA-MM-DD)")
    parser.add_argument("--saida", help="diretório de publicação (padrão: EXPORTACOES_DIR)")
    parser.add_argument("--lote", type=int, default=5000, help="linhas por lote")
    args = parser.parse_args()
    app = create_app()
    with app.app_context():
        destino = Path(args.saida or diretorio_exportacoes()) / nome_arquivo(args.data)
        resultado = gerar_parquet_diario(args.data, destino, tamanho_lote=args.lote)
    print(f"{resultado['registros']} registros publicados em {resultado['arquivo']}")


if __name__ == "__main__":
    main()
