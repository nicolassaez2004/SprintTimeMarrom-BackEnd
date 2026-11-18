"""Executar explicitamente: python -m app.tarefas."""

import argparse
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app import create_app, db
from app.coleta.bus_collector import LockColetor
from app.gtfs.scheduler import (
    executar_tarefas, nome_lock_gtfs, proxima_verificacao,
    recuperar_interrompidas, verificar_gtfs, agora_utc,
)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Verificação diária do GTFS")
    parser.add_argument("--uma-vez", action="store_true",
                        help="Executa uma verificação agora e encerra, sem agendar o sistema operacional")
    args = parser.parse_args(argv)
    app = create_app()
    caminho = Path(app.config.get("GTFS_TASK_LOG_FILE") or
                   (Path(app.instance_path) / "logs" / "gtfs-tarefas.jsonl"))
    caminho.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(caminho, maxBytes=10 * 1024 * 1024,
                                  backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger("app.gtfs.scheduler")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    logging.basicConfig(level=logging.INFO)
    lock = None
    try:
        with app.app_context():
            lock = LockColetor(db.engine, nome=nome_lock_gtfs(db.engine))
            if not lock.adquirir():
                logger.error('{"evento":"gtfs_processo_ja_ativo"}')
                return 1
            if args.uma_vez:
                agora = agora_utc()
                recuperar_interrompidas(agora)
                prevista = min(agora, proxima_verificacao(agora))
                resultado = verificar_gtfs(prevista)
                return 0 if resultado["estado"] == "OK" else 1
            executar_tarefas(manter_ativo=lock.ativo)
            return 1  # O loop contínuo só retorna se perder o lock.
    except KeyboardInterrupt:
        return 0
    except Exception as erro:
        # Exceções de SQL/HTTP podem conter URLs e senhas: registrar só o tipo.
        logger.error('{"evento":"gtfs_processo_falha","tipo_erro":"%s"}',
                     type(erro).__name__)
        return 1
    finally:
        if lock is not None:
            lock.liberar()
        logger.removeHandler(handler)
        handler.close()


if __name__ == "__main__":
    raise SystemExit(main())
