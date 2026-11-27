"""Worker separado do Flask HTTP: ``python -m app.jobs [--uma-vez]``.

Ctrl+C / SIGTERM sinalizam a parada; o job em andamento termina antes de sair.
"""
import argparse
import logging
import signal
from threading import Event

from app import create_app
from app.exportacoes.extracoes import TIPO as EXTRACAO, executor_extracao
from app.reprocessamento.execucao import executor_recalculo
from app.reprocessamento.pedidos import TIPO as RECALCULO

# tipo -> fábrica do executor (US20-T03: cálculo, publicação, versão e caches;
# US28-T02: extração de brutos)
TAREFAS = {RECALCULO: executor_recalculo, EXTRACAO: executor_extracao}


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
    feitos = TAREFAS[args.tipo](create_app()).executar(parar, args.intervalo, uma_vez=args.uma_vez)
    logging.getLogger(__name__).info('{"evento":"worker_fim","jobs":%d,"erros":%d}',
                                     len(feitos), sum(e == "ERRO" for _, e in feitos))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
