"""GET /posicoes/atuais: mapa ao vivo (US22), público e sem chamar a SPTrans."""

import re

from flask import Blueprint, current_app, request
from sqlalchemy.exc import SQLAlchemyError

from app import db
from app.consultas.posicoes import ultimas_posicoes

posicoes_bp = Blueprint("posicoes", __name__)


def _erro(codigo, mensagem, status, campo=None):
    detalhe = {"codigo": codigo, "mensagem": mensagem}
    if campo is not None:
        detalhe["campo"] = campo
    return {"erro": detalhe}, status


def _ler_filtros(parametros):
    for campo in parametros:
        if campo not in ("linha", "linha_gtfs"):
            return None, _erro("FILTRO_INVALIDO", "Filtro não reconhecido.", 400, campo)
        if len(parametros.getlist(campo)) != 1:
            return None, _erro("FILTRO_INVALIDO", "Informe cada filtro uma única vez.", 400, campo)
    linha = parametros.get("linha")
    if linha is not None and not re.fullmatch(r"[0-9]{1,16}", linha):
        return None, _erro("FILTRO_INVALIDO",
                           "Informe o código numérico da linha na Olho Vivo (campo cl).", 400, "linha")
    gtfs = parametros.get("linha_gtfs")
    # route_id GTFS é textual e sensível a maiúsculas e espaços no modelo.
    if gtfs is not None and (not gtfs or len(gtfs) > 64
                             or any(ord(c) < 32 or ord(c) == 127 for c in gtfs)):
        return None, _erro("FILTRO_INVALIDO",
                           "Informe um código GTFS com até 64 caracteres, sem controles.", 400, "linha_gtfs")
    return {"linha": linha, "linha_gtfs": gtfs}, None


@posicoes_bp.get("/posicoes/atuais")
@posicoes_bp.get("/api/posicoes/atuais")
def posicoes_atuais():
    filtros, erro = _ler_filtros(request.args)
    if erro:
        return erro
    try:
        return ultimas_posicoes(**filtros), 200
    except SQLAlchemyError as falha:
        db.session.rollback()
        # A mensagem da exceção pode conter SQL ou credenciais da conexão.
        current_app.logger.error("Falha na consulta de posições (%s)", type(falha).__name__)
        return _erro("CONSULTA_INDISPONIVEL", "Não foi possível consultar as posições. Tente novamente.", 503)
