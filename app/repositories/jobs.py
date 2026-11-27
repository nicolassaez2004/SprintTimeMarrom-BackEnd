"""Leitura e gravação de ``ExecucaoProcessamento`` (jobs duráveis); sem commit."""
from sqlalchemy import or_

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
