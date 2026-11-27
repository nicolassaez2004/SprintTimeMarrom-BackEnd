"""JobService: registra pedidos duráveis de trabalho em ``ExecucaoProcessamento``.

Diagrama de classes (p. 3/5): pedido durável com versões, chave lógica única,
tentativas e reserva com expiração, para cálculo, importação, exportação e
backup (US20, US28). O estado fica no MySQL e sobrevive ao reinício.

Registra e lista pedidos (US20-T02A) e controla a posse de cada execução
(US20-T02B): ``reservar`` confirma a posse numa transação curta e ``concluir`` /
``falhar`` só alteram o job enquanto o ``reserva_token`` ainda for o do executor.
"""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy.exc import IntegrityError, OperationalError

from app import db
from app.models import ExecucaoProcessamento
from app.repositories import jobs as repositorio


def _conflito(erro):
    """Unicidade, deadlock (1213) ou espera de lock (1205): repetir é seguro."""
    codigo = getattr(getattr(erro, "orig", None), "args", (None,))[0]
    return isinstance(erro, IntegrityError) or (
        isinstance(erro, OperationalError) and codigo in (1205, 1213))


class JobService:
    TENTATIVAS = 3
    RESERVA = timedelta(minutes=15)

    def solicitar(self, pedidos):
        """Cria ou reutiliza um job por ``chave_logica``, todos numa transação.

        ``pedidos`` são dicts com os campos de ``ExecucaoProcessamento``. Novos
        jobs nascem ``AGUARDANDO`` com 0 tentativas; existentes, em qualquer
        estado, são devolvidos sem alteração.

        A consulta prévia só evita INSERTs inúteis. Quem decide é o ``UNIQUE``:
        se um concorrente confirmar a mesma chave antes do nosso INSERT, ou se
        o InnoDB abortar por deadlock, o lote é desfeito e repetido numa
        transação nova, que já enxerga o job vencedor. Outras falhas (ex.: FK)
        desfazem o lote e são propagadas.
        """
        por_chave = {p["chave_logica"]: p for p in pedidos}
        if not por_chave:
            raise ValueError("nenhum pedido informado")
        for tentativa in range(1, self.TENTATIVAS + 1):
            try:
                # Ordem fixa reduz deadlocks entre lotes concorrentes.
                jobs = {chave: repositorio.obter_por_chave(chave) or repositorio.adicionar(
                            ExecucaoProcessamento(estado="AGUARDANDO", tentativas=0, **campos))
                        for chave, campos in sorted(por_chave.items())}
                db.session.commit()
                return [jobs[p["chave_logica"]] for p in pedidos]
            except Exception as erro:
                db.session.rollback()
                if tentativa == self.TENTATIVAS or not _conflito(erro):
                    raise

    def pendentes(self, tipo, agora=None):
        """Jobs a retomar após reinício: aguardando, com erro ou reserva vencida."""
        return repositorio.pendentes(tipo, agora or datetime.now(timezone.utc))

    def reservar(self, tipo, agora=None, duracao=None):
        """Reserva atomicamente o job mais antigo reservável e confirma; ou ``None``.

        Reserváveis: ``AGUARDANDO`` e ``PROCESSANDO`` com reserva vencida.
        ``ERRO`` não é retomado aqui: limite de tentativas e espera entre elas
        são política de retomada (tarefa posterior).
        """
        agora = agora or datetime.now(timezone.utc)
        for job_id in repositorio.candidatos(tipo, agora):
            token = str(uuid4())
            if repositorio.reservar(job_id, token, agora, agora + (duracao or self.RESERVA)):
                db.session.commit()
                return db.session.get(ExecucaoProcessamento, job_id)
            db.session.rollback()  # outro executor venceu; encerra a leitura e tenta o próximo
        db.session.rollback()
        return None

    def concluir(self, job_id, token, agora=None):
        """Marca PRONTO **sem commit**, na mesma transação dos resultados.

        ``False`` = posse perdida; o chamador deve desfazer os resultados.
        """
        return repositorio.encerrar(job_id, token, "PRONTO", agora or datetime.now(timezone.utc))

    def falhar(self, job_id, token, erro, agora=None):
        """Marca ERRO (só o tipo da exceção: mensagens de SQL podem conter senhas) e confirma."""
        ok = repositorio.encerrar(job_id, token, "ERRO", agora or datetime.now(timezone.utc),
                                  erro=type(erro).__name__[:128])
        db.session.commit()
        return ok
