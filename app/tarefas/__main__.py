"""Executar explicitamente: python -m app.tarefas."""

import argparse
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app import create_app, db
from app.coleta.bus_collector import LockColetor
from app.contexto.clima.coleta import coletar_clima
from app.exportacoes.parquet import publicar_brutos
from app.indicadores.agenda import atualizar_indicadores
from app.viagens.cadeia import processar_cadeia
from app.contexto.infraestrutura.tarefa import atualizar_infraestrutura
from app.gtfs.scheduler import (
    executar_tarefas, nome_lock_gtfs, proxima_verificacao,
    recuperar_interrompidas, verificar_gtfs,
)
from app.models import agora_utc


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Verificação diária do GTFS, publicação dos brutos em Parquet e "
                    "indicadores a cada 15 min com consolidação diária (US23-T03) e clima por trecho (US06)")
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
    loggers = [logger, logging.getLogger("app.exportacoes.parquet"),
               logging.getLogger("app.indicadores.agenda"),
               logging.getLogger("app.contexto.clima.coleta"), logging.getLogger("app.viagens.cadeia")]
    for registrador in loggers:
        registrador.setLevel(logging.INFO)
        registrador.addHandler(handler)
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
            # A cadeia (correspondência → partidas → viagens → passagens) roda antes da
            # agenda de indicadores, que passa a encontrar viagens reconstruídas.
            executar_tarefas(manter_ativo=lock.ativo, periodicas=(
                publicar_brutos, processar_cadeia, atualizar_indicadores, atualizar_infraestrutura, coletar_clima
            ))

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
        for registrador in loggers:
            registrador.removeHandler(handler)
        handler.close()


if __name__ == "__main__":
    raise SystemExit(main())
