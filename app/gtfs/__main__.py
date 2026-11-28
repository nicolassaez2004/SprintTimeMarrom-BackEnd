"""Executa uma captura manual e importa a rede: python -m app.gtfs

``python -m app.gtfs corrigir-vigencias`` encerra, nas versões já gravadas, as
vigências abertas que uma captura mais nova substituiu (US02).
"""
import sys

from app import create_app, db
from app.gtfs.service import capturar_gtfs, corrigir_vigencias_abertas, importar_linhas_paradas, importar_trechos


def main():
    app = create_app()
    with app.app_context():
        if sys.argv[1:] == ["corrigir-vigencias"]:
            total = corrigir_vigencias_abertas()
            db.session.commit()
            print(f"Vigências encerradas: {total}")
            return
        versao = capturar_gtfs()
        print(f"Versão GTFS {versao.id} | SHA-256 {versao.hash}")
        rede = importar_linhas_paradas(versao.id)
        print(f"Linhas {rede['linhas']} | Paradas {rede['paradas']}")
        print(f"Trechos {importar_trechos(versao.id)['trechos']}")


if __name__ == "__main__":
    main()
