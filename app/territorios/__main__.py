"""Import GeoSampa boundaries and optionally associate one GTFS version."""

import argparse
import json

from app import create_app
from app.territorios.associacao import associar_rede_a_territorios
from app.territorios.importacao import importar_snapshot_geosampa


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--versao-gtfs",
        help="UUID da versão de rede a associar aos limites importados",
    )
    argumentos = parser.parse_args()

    app = create_app()
    with app.app_context():
        versao = importar_snapshot_geosampa()
        saida = {
            "versao_territorial_id": versao.id,
            "versao_fonte": versao.versao_fonte,
            "sha256": versao.sha256,
        }
        if argumentos.versao_gtfs:
            resultado = associar_rede_a_territorios(argumentos.versao_gtfs, versao.id)
            saida["associacao"] = resultado.__dict__
        print(json.dumps(saida, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
