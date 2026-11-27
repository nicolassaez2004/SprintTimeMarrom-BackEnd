"""Monitor externo: python -m scripts.sondagem --api URL --painel URL."""
import argparse
import logging
import time

from sqlalchemy.exc import SQLAlchemyError
from app import create_app, db
from app.monitoramento.sondagem import sondar_e_gravar


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", required=True)
    parser.add_argument("--painel", required=True)
    parser.add_argument("--texto-api")
    parser.add_argument("--texto-painel")
    parser.add_argument("--timeout", type=float, default=5)
    parser.add_argument("--uma-vez", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    app = create_app()
    with app.app_context():
        proxima = time.monotonic()
        while True:
            try:
                medicoes = sondar_e_gravar(args.api, args.painel, texto_api=args.texto_api,
                                           texto_painel=args.texto_painel, timeout=args.timeout)
                logging.info("Sondagem registrada: %s", ", ".join(f"{m.alvo}={m.resultado}" for m in medicoes))
            except SQLAlchemyError as erro:
                db.session.rollback()
                logging.error("Falha ao persistir sondagem (%s)", type(erro).__name__)
            if args.uma_vez:
                break
            proxima += 60
            agora = time.monotonic()
            if proxima <= agora:
                proxima += (int((agora - proxima) // 60) + 1) * 60
            time.sleep(max(0, proxima - time.monotonic()))


if __name__ == "__main__":
    main()
