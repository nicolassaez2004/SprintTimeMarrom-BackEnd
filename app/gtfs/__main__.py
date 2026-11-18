"""Executa uma captura manual e importa a rede: python -m app.gtfs"""

from app import create_app
from app.gtfs.service import capturar_gtfs, importar_linhas_paradas, importar_trechos


def main():
    app = create_app()
    with app.app_context():
        versao = capturar_gtfs()
        print(f"Versão GTFS {versao.id} | SHA-256 {versao.hash}")
        rede = importar_linhas_paradas(versao.id)
        print(f"Linhas {rede['linhas']} | Paradas {rede['paradas']}")
        print(f"Trechos {importar_trechos(versao.id)['trechos']}")


if __name__ == "__main__":
    main()
