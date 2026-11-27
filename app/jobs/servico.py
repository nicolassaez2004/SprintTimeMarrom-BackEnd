"""JobService: registra pedidos duráveis de trabalho em ``ExecucaoProcessamento``.

Diagrama de classes (p. 3/5): pedido durável com versões, chave lógica única,
tentativas e reserva com expiração, para cálculo, importação, exportação e
backup (US20, US28). O estado fica no MySQL e sobrevive ao reinício.

Este serviço só **registra** e **lista para retomada**. A reserva atômica e a
execução pertencem ao worker; hoje existem em ``app.persistencia.reprocessamento``.
"""
from datetime import datetime, timezone

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
