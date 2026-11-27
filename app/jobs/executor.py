"""Worker de jobs duráveis (diagrama de componentes: "Worker + jobs duráveis").

Processo separado do Flask HTTP. Cada execução empurra um ``app_context``
próprio; como o Flask-SQLAlchemy escopa ``db.session`` pelo contexto, cada
execução tem sua sessão, que é removida (rollback + close) ao final.

Fluxo de uma execução:
1. ``JobService.reservar`` — transação curta, confirmada antes do cálculo
   (registra antes as execuções interrompidas por reserva vencida).
2. ``tarefa(job)`` — cálculo pesado, só leituras; retorna os resultados.
   Com ``publicar`` (US20-T02C), a transação de leitura é encerrada aqui.
3. ``JobService.assumir_publicacao`` — ``SELECT … FOR UPDATE`` exigindo o
   ``reserva_token``; sem posse, nada é gravado (``PossePerdida``).
4. ``publicar(job, resultados)`` + ``JobService.concluir`` + commit —
   resultados e PRONTO juntos numa transação curta.
5. Em falha: rollback e ``JobService.falhar`` (ERRO com o tipo do erro).

Sem ``publicar`` (contrato da T02B), ``tarefa`` grava na sessão sem commit e
só ``concluir`` valida a posse, ainda na mesma transação dos resultados.
"""
from datetime import datetime, timezone
import logging
from threading import Event

from app import db
from app.jobs.servico import JobService

logger = logging.getLogger(__name__)


class PossePerdida(RuntimeError):
    """A reserva venceu e outro executor assumiu o job; resultados descartados."""


class Executor:
    def __init__(self, app, tipo, tarefa, publicar=None, *, duracao_reserva=None, relogio=None,
                 servico=None):
        self.app, self.tipo, self.tarefa, self.publicar = app, tipo, tarefa, publicar
        self.duracao = duracao_reserva
        self.relogio = relogio or (lambda: datetime.now(timezone.utc))
        self.servico = servico or JobService()

    def executar_um(self):
        """Reserva e executa um job. Retorna ``(id, estado)`` ou ``None`` se não há job livre."""
        servico = self.servico
        with self.app.app_context():
            try:
                job = servico.reservar(self.tipo, self.relogio(), self.duracao)
                if job is None:
                    return None
                job_id, token = job.id, job.reserva_token
                try:
                    resultados = self.tarefa(job)
                    if self.publicar:
                        db.session.rollback()  # fecha a leitura; nada foi gravado
                        job = servico.assumir_publicacao(job_id, token)
                        if job is None:
                            raise PossePerdida(job_id)
                        self.publicar(job, resultados)
                    if not servico.concluir(job_id, token, self.relogio()):
                        raise PossePerdida(job_id)
                    db.session.commit()
                    return job_id, "PRONTO"
                except Exception as erro:
                    db.session.rollback()
                    servico.falhar(job_id, token, erro, self.relogio())
                    logger.error('{"evento":"job_falha","tipo":"%s","job":"%s","erro":"%s"}',
                                 self.tipo, job_id, type(erro).__name__)
                    return job_id, "ERRO"
            finally:
                db.session.remove()

    def executar(self, parar=None, intervalo=5.0, uma_vez=False):
        """Loop até ``parar`` ser sinalizado; ``uma_vez`` esvazia a fila e retorna."""
        parar = parar or Event()
        feitos = []
        while not parar.is_set():
            resultado = self.executar_um()
            if resultado is not None:
                feitos.append(resultado)
            elif uma_vez:
                break
            else:
                parar.wait(intervalo)
        return feitos
