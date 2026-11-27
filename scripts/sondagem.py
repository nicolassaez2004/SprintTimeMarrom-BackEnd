"""US30-T01B — monitor externo, uma rodada por minuto:

    python -m scripts.sondagem --api URL --painel URL [--texto-api T] [--texto-painel T]

Alvos também por ambiente: SAUDE_URL_API, SAUDE_URL_PAINEL, SAUDE_TEXTO_API,
SAUDE_TEXTO_PAINEL. Grava no banco de DATABASE_URL; deve rodar fora do processo
Flask (e, para medir queda da máquina, noutra máquina). Interromper o monitor
deixa minutos sem medição: eles aparecem como lacuna, nunca como sucesso.
"""
import argparse
import logging
import os
import time

from app import create_app, db
from app.monitoramento.sondagem import sondar_e_gravar


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api", default=os.getenv("SAUDE_URL_API"))
    parser.add_argument("--painel", default=os.getenv("SAUDE_URL_PAINEL"))
    parser.add_argument("--texto-api", default=os.getenv("SAUDE_TEXTO_API") or None)
    parser.add_argument("--texto-painel", default=os.getenv("SAUDE_TEXTO_PAINEL") or None)
    parser.add_argument("--timeout", type=float, default=5)
    parser.add_argument("--uma-vez", action="store_true")
    args = parser.parse_args(argv)
    if not args.api or not args.painel:
        parser.error("informe --api e --painel (ou SAUDE_URL_API e SAUDE_URL_PAINEL)")
    alvos = {"API": (args.api, args.texto_api), "PAINEL": (args.painel, args.texto_painel)}
    logging.basicConfig(level=logging.INFO)
    app = create_app()
    proxima = time.monotonic()
    while True:
        with app.app_context():
            try:
                medicoes = sondar_e_gravar(alvos, timeout=args.timeout)
                logging.info("Sondagem: %s", ", ".join(
                    f"{c}={m.detalhe if m else 'NAO_GRAVADA'}" for c, m in medicoes.items()))
            finally:
                db.session.remove()
        if args.uma_vez:
            return 0
        proxima += 60
        agora = time.monotonic()
        if proxima <= agora:  # rodada atrasada: pula para o próximo minuto, sem rajada
            proxima += (int((agora - proxima) // 60) + 1) * 60
        time.sleep(max(0, proxima - time.monotonic()))


if __name__ == "__main__":
    raise SystemExit(main())
