"""Leitura e gravação de ``ExecucaoProcessamento`` (jobs duráveis); sem commit."""
from sqlalchemy import or_, update

from app import db
from app.models import ExecucaoProcessamento


def obter_por_chave(chave_logica):
    return ExecucaoProcessamento.query.filter_by(chave_logica=chave_logica).one_or_none()


def adicionar(execucao):
    db.session.add(execucao)
    return execucao


def _reservavel(agora, limite, espera):
    """AGUARDANDO, ou ERRO com tentativas restantes e espera cumprida (US20-T02C).

    PROCESSANDO nunca é reservável: reserva vencida passa antes por
    ``interromper``, que a converte em ERRO e fecha a tentativa no histórico.
    """
    E = ExecucaoProcessamento
    return or_(E.estado == "AGUARDANDO", (E.estado == "ERRO") & (E.tentativas < limite) & (
        E.finalizada_em.is_(None) | (E.finalizada_em <= agora - espera)))


def _vencida(agora):
    E = ExecucaoProcessamento
    return (E.estado == "PROCESSANDO") & (E.reserva_ate.is_(None) | (E.reserva_ate <= agora))


def pendentes(tipo, agora, limite):
    """A retomar após reinício: aguardando, ERRO com tentativas restantes ou reserva vencida."""
    E = ExecucaoProcessamento
    return (E.query.filter(E.tipo == tipo, or_(
        E.estado == "AGUARDANDO", (E.estado == "ERRO") & (E.tentativas < limite), _vencida(agora),
    )).order_by(E.criada_em, E.id).all())


def vencidos(tipo, agora):
    """Ids em PROCESSANDO cuja reserva venceu: execução interrompida."""
    E = ExecucaoProcessamento
    return [i for (i,) in db.session.query(E.id).filter(E.tipo == tipo, _vencida(agora))
            .order_by(E.criada_em, E.id)]


def interromper(job_id, agora):
    """UPDATE condicional PROCESSANDO vencido -> ERRO ``ReservaExpirada``; só um executor vence."""
    E = ExecucaoProcessamento
    return db.session.execute(update(E).where(E.id == job_id, _vencida(agora)).values(
        estado="ERRO", finalizada_em=agora, erro="ReservaExpirada",
        reserva_token=None, reserva_ate=None,
    ).execution_options(synchronize_session=False)).rowcount == 1


def candidatos(tipo, agora, limite, espera, quantos=20):
    """Ids reserváveis, do mais antigo; leitura otimista, quem decide é ``reservar``."""
    E = ExecucaoProcessamento
    return [i for (i,) in db.session.query(E.id).filter(E.tipo == tipo, _reservavel(
        agora, limite, espera)).order_by(E.criada_em, E.id).limit(quantos)]


def reservar(job_id, token, agora, ate, limite, espera):
    """UPDATE condicional: o InnoDB bloqueia a linha e reavalia o WHERE sobre a
    versão confirmada, então só um executor obtém ``rowcount == 1``."""
    E = ExecucaoProcessamento
    return db.session.execute(update(E).where(E.id == job_id, _reservavel(agora, limite, espera)).values(
        estado="PROCESSANDO", tentativas=E.tentativas + 1,
        iniciada_em=agora, finalizada_em=None, erro=None,
        reserva_token=token, reserva_ate=ate,
    ).execution_options(synchronize_session=False)).rowcount == 1


def travar_se_dono(job_id, token):
    """``SELECT … FOR UPDATE`` do job ainda em posse de ``token``; ``None`` se a perdeu.

    Mantém a linha bloqueada até o fim da transação: nenhum outro executor
    interrompe ou reserva o job enquanto os resultados são publicados.
    """
    E = ExecucaoProcessamento
    return (E.query.filter(E.id == job_id, E.estado == "PROCESSANDO", E.reserva_token == token)
            .with_for_update().populate_existing().one_or_none())


def encerrar(job_id, token, estado, agora, erro=None):
    """Grava PRONTO/ERRO somente se ``token`` ainda detém a reserva."""
    E = ExecucaoProcessamento
    return db.session.execute(update(E).where(
        E.id == job_id, E.estado == "PROCESSANDO", E.reserva_token == token,
    ).values(
        estado=estado, finalizada_em=agora, erro=erro, reserva_token=None, reserva_ate=None,
    ).execution_options(synchronize_session=False)).rowcount == 1


def registrar_tentativa(job_id, **campos):
    """Abre (``tentativa`` presente) ou fecha a última tentativa em ``historico``.

    Só é chamado depois de um UPDATE condicional vencedor na mesma transação:
    a linha já está bloqueada, então ler-modificar-gravar o JSON é seguro.
    """
    job = db.session.get(ExecucaoProcessamento, job_id, populate_existing=True)
    historico = [dict(t) for t in job.historico or []]
    if "tentativa" in campos:
        historico.append({"fim": None, "erro": None, **campos})
    elif historico:
        historico[-1].update(campos)
    job.historico = historico
    return job
