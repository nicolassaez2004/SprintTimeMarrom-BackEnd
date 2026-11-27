"""Leitura e gravação de ``ExecucaoProcessamento`` (jobs duráveis); sem commit."""
from sqlalchemy import or_, update

from app import db
from app.models import ExecucaoProcessamento


def obter_por_chave(chave_logica):
    return ExecucaoProcessamento.query.filter_by(chave_logica=chave_logica).one_or_none()


def adicionar(execucao):
    db.session.add(execucao)
    return execucao


def pendentes(tipo, agora):
    """Aguardando, com erro ou em processamento com reserva vencida."""
    E = ExecucaoProcessamento
    return (E.query.filter(E.tipo == tipo, or_(
        E.estado.in_(("AGUARDANDO", "ERRO")),
        (E.estado == "PROCESSANDO") & (E.reserva_ate.is_(None) | (E.reserva_ate <= agora)),
    )).order_by(E.criada_em, E.id).all())


def candidatos(tipo, agora, limite=20):
    """Ids reserváveis: aguardando ou em processamento com reserva vencida.

    É só uma leitura otimista; quem decide a posse é ``reservar``.
    """
    E = ExecucaoProcessamento
    return [i for (i,) in db.session.query(E.id).filter(E.tipo == tipo, or_(
        E.estado == "AGUARDANDO",
        (E.estado == "PROCESSANDO") & (E.reserva_ate.is_(None) | (E.reserva_ate <= agora)),
    )).order_by(E.criada_em, E.id).limit(limite)]


def reservar(job_id, token, agora, ate):
    """UPDATE condicional: o InnoDB bloqueia a linha e reavalia o WHERE sobre a
    versão confirmada, então só um executor obtém ``rowcount == 1``."""
    E = ExecucaoProcessamento
    return db.session.execute(update(E).where(E.id == job_id, or_(
        E.estado == "AGUARDANDO",
        (E.estado == "PROCESSANDO") & (E.reserva_ate.is_(None) | (E.reserva_ate <= agora)),
    )).values(
        estado="PROCESSANDO", tentativas=E.tentativas + 1,
        iniciada_em=agora, finalizada_em=None, erro=None,
        reserva_token=token, reserva_ate=ate,
    ).execution_options(synchronize_session=False)).rowcount == 1


def encerrar(job_id, token, estado, agora, erro=None):
    """Grava PRONTO/ERRO somente se ``token`` ainda detém a reserva."""
    E = ExecucaoProcessamento
    return db.session.execute(update(E).where(
        E.id == job_id, E.estado == "PROCESSANDO", E.reserva_token == token,
    ).values(
        estado=estado, finalizada_em=agora, erro=erro, reserva_token=None, reserva_ate=None,
    ).execution_options(synchronize_session=False)).rowcount == 1
