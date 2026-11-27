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

US20-T03: ``preservar(job_id)`` roda depois de ``concluir`` e antes do commit
(o PRONTO e ``finalizada_em`` já estão na transação); falhar nele desfaz tudo.
``apos_confirmar(job_id)`` roda depois do commit (invalidação de caches); sua
falha é registrada e não desfaz a publicação já confirmada.
"""
import logging
from datetime import datetime, timezone
from threading import Event

from app import db
from app.jobs.servico import JobService

logger = logging.getLogger(__name__)


class PossePerdida(RuntimeError):
    """A reserva venceu e outro executor assumiu o job; resultados descartados."""


class Executor:
    def __init__(self, app, tipo, tarefa, publicar=None, *, preservar=None, apos_confirmar=None,
                 duracao_reserva=None, relogio=None, servico=None):
        self.app, self.tipo, self.tarefa, self.publicar = app, tipo, tarefa, publicar
        self.preservar, self.apos_confirmar = preservar, apos_confirmar
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
                    if self.preservar:
                        self.preservar(job_id)
                    db.session.commit()
                except Exception as erro:
                    db.session.rollback()
                    servico.falhar(job_id, token, erro, self.relogio())
                    logger.error('{"evento":"job_falha","tipo":"%s","job":"%s","erro":"%s"}',
                                 self.tipo, job_id, type(erro).__name__)
                    return job_id, "ERRO"
                if self.apos_confirmar:
                    try:
                        self.apos_confirmar(job_id)
                    except Exception as erro:  # publicação já confirmada; só registra
                        db.session.rollback()
                        logger.error('{"evento":"job_pos_commit_falha","tipo":"%s","job":"%s","erro":"%s"}',
                                     self.tipo, job_id, type(erro).__name__)
                return job_id, "PRONTO"
            finally:
                db.session.remove()

    def executar(self, parar=None, intervalo=5.0, uma_vez=False):
        """Loop até ``parar`` ser sinalizado; ``uma_vez`` esvazia a fila e retorna.

        Falha de infraestrutura fora do job (banco indisponível ao reservar ou ao
        registrar ERRO) não encerra o worker contínuo: registra, espera
        ``intervalo`` e tenta de novo. Um job que ficou PROCESSANDO é recuperado
        quando a reserva vencer (US20-T02C). Em ``uma_vez`` a falha é propagada.
        """
        parar = parar or Event()
        feitos = []
        while not parar.is_set():
            try:
                resultado = self.executar_um()
            except Exception as erro:
                if uma_vez:
                    raise
                logger.error('{"evento":"worker_falha_infra","tipo":"%s","erro":"%s"}',
                             self.tipo, type(erro).__name__)
                parar.wait(intervalo)
                continue
            if resultado is not None:
                feitos.append(resultado)
            elif uma_vez:
                break
            else:
                parar.wait(intervalo)
        return feitos
