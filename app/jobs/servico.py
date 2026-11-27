"""JobService: registra pedidos duráveis de trabalho em ``ExecucaoProcessamento``.

Diagrama de classes (p. 3/5): pedido durável com versões, chave lógica única,
tentativas e reserva com expiração, para cálculo, importação, exportação e
backup (US20, US28). O estado fica no MySQL e sobrevive ao reinício.

Registra e lista pedidos (US20-T02A) e controla a posse de cada execução
(US20-T02B): ``reservar`` confirma a posse numa transação curta e ``concluir`` /
``falhar`` só alteram o job enquanto o ``reserva_token`` ainda for o do executor.

Retomada (US20-T02C): reserva vencida = execução interrompida, registrada como
ERRO ``ReservaExpirada``; ERRO volta a ser reservado até ``limite`` tentativas,
após ``espera``. Cada tentativa fica em ``historico`` (início, fim, estado, erro).
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


def _iso(instante):
    return instante.astimezone(timezone.utc).isoformat()


class JobService:
    TENTATIVAS = 3  # repetições de ``solicitar`` em conflito de unicidade/deadlock
    RESERVA = timedelta(minutes=15)
    LIMITE_TENTATIVAS = 3  # execuções por job (US20-T02C; valor a confirmar pelo PO)
    ESPERA_RETOMADA = timedelta(0)  # entre uma falha e a nova tentativa (idem)

    def __init__(self, limite=None, espera=None):
        self.limite = self.LIMITE_TENTATIVAS if limite is None else limite
        self.espera = self.ESPERA_RETOMADA if espera is None else espera

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
        """Jobs a retomar após reinício: aguardando, ERRO com tentativas restantes ou reserva vencida."""
        return repositorio.pendentes(tipo, agora or datetime.now(timezone.utc), self.limite)

    def recuperar_interrompidos(self, tipo, agora=None):
        """Converte cada PROCESSANDO com reserva vencida em ERRO ``ReservaExpirada``.

        Uma transação curta por job; o UPDATE condicional garante que só um
        executor registra a interrupção. Retorna os ids recuperados.
        """
        agora = agora or datetime.now(timezone.utc)
        recuperados = []
        for job_id in repositorio.vencidos(tipo, agora):
            if repositorio.interromper(job_id, agora):
                repositorio.registrar_tentativa(job_id, fim=_iso(agora), estado="INTERROMPIDA",
                                                erro="ReservaExpirada")
                recuperados.append(job_id)
            db.session.commit()
        return recuperados

    def reservar(self, tipo, agora=None, duracao=None):
        """Reserva atomicamente o job mais antigo reservável e confirma; ou ``None``.

        Antes, registra as execuções interrompidas. Reserváveis: ``AGUARDANDO`` e
        ``ERRO`` com menos de ``limite`` tentativas e ``espera`` cumprida.
        """
        agora = agora or datetime.now(timezone.utc)
        self.recuperar_interrompidos(tipo, agora)
        for job_id in repositorio.candidatos(tipo, agora, self.limite, self.espera):
            token = str(uuid4())
            if repositorio.reservar(job_id, token, agora, agora + (duracao or self.RESERVA),
                                    self.limite, self.espera):
                job = db.session.get(ExecucaoProcessamento, job_id, populate_existing=True)
                repositorio.registrar_tentativa(job_id, tentativa=job.tentativas,
                                                inicio=_iso(agora), estado="PROCESSANDO")
                db.session.commit()
                return job
            db.session.rollback()  # outro executor venceu; encerra a leitura e tenta o próximo
        db.session.rollback()
        return None

    def assumir_publicacao(self, job_id, token):
        """Bloqueia o job (``FOR UPDATE``) se ``token`` ainda tem a posse; senão ``None``.

        Abre a transação de publicação: até o commit, nenhum outro executor
        interrompe ou reserva o job. Resultados e PRONTO entram juntos.
        """
        return repositorio.travar_se_dono(job_id, token)

    def concluir(self, job_id, token, agora=None):
        """Marca PRONTO **sem commit**, na mesma transação dos resultados.

        ``False`` = posse perdida; o chamador deve desfazer os resultados.
        """
        agora = agora or datetime.now(timezone.utc)
        if not repositorio.encerrar(job_id, token, "PRONTO", agora):
            return False
        repositorio.registrar_tentativa(job_id, fim=_iso(agora), estado="PRONTO")
        return True

    def falhar(self, job_id, token, erro, agora=None):
        """Marca ERRO (só o tipo da exceção: mensagens de SQL podem conter senhas) e confirma."""
        agora = agora or datetime.now(timezone.utc)
        nome = type(erro).__name__[:128]
        ok = repositorio.encerrar(job_id, token, "ERRO", agora, erro=nome)
        if ok:
            repositorio.registrar_tentativa(job_id, fim=_iso(agora), estado="ERRO", erro=nome)
        db.session.commit()
        return ok
