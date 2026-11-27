"""Worker separado do Flask HTTP: ``python -m app.jobs [--uma-vez]``.

Ctrl+C / SIGTERM sinalizam a parada; o job em andamento termina antes de sair.
"""
import argparse
import logging
import signal
from threading import Event

from app import create_app
from app.jobs.executor import Executor
from app.reprocessamento.execucao import calcular_pedido, publicar_pedido
from app.reprocessamento.pedidos import TIPO as RECALCULO

TAREFAS = {RECALCULO: (calcular_pedido, publicar_pedido)}  # (cálculo, publicação)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Executa jobs duráveis de ExecucaoProcessamento")
    parser.add_argument("--tipo", choices=sorted(TAREFAS), default=RECALCULO)
    parser.add_argument("--uma-vez", action="store_true",
                        help="Executa os jobs disponíveis e encerra")
    parser.add_argument("--intervalo", type=float, default=5.0,
                        help="Segundos de espera quando não há job (padrão 5)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    parar = Event()
    for sinal in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sinal, lambda *_: parar.set())
    feitos = Executor(create_app(), args.tipo, *TAREFAS[args.tipo]).executar(
        parar, args.intervalo, uma_vez=args.uma_vez)
    logging.getLogger(__name__).info('{"evento":"worker_fim","jobs":%d,"erros":%d}',
                                     len(feitos), sum(e == "ERRO" for _, e in feitos))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
