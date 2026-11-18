"""Executa uma captura manual: python -m app.gtfs"""

from app import create_app
from app.gtfs.service import capturar_gtfs


def main():
    app = create_app()
    with app.app_context():
        versao = capturar_gtfs()
        print(f"Versão GTFS {versao.id} | SHA-256 {versao.hash}")


if __name__ == "__main__":
    main()
