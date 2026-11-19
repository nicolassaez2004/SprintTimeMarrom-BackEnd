"""GET /partidas: consulta das saídas já persistidas, com procedência."""

from flask import Blueprint, current_app, request
from sqlalchemy.exc import SQLAlchemyError

from app import db
from app.gtfs.service import VersaoGTFSAmbigua, VersaoGTFSInexistente
from app.programacao.schemas import FiltroInvalido, ler_filtros
from app.programacao.service import LinhaNaoEncontrada, ProgramacaoAmbigua, consultar_partidas

partidas_bp = Blueprint("partidas", __name__)


def _erro(codigo, mensagem, status, campo=None):
    detalhe = {"codigo": codigo, "mensagem": mensagem}
    if campo is not None:
        detalhe["campo"] = campo
    return {"erro": detalhe}, status


@partidas_bp.get("/partidas")
@partidas_bp.get("/api/partidas")
def partidas():
    try:
        filtros = ler_filtros(request.args)
    except FiltroInvalido as erro:
        return _erro("FILTRO_INVALIDO", str(erro), 400, erro.campo)
    try:
        return consultar_partidas(filtros), 200
    except VersaoGTFSInexistente:
        return _erro("VERSAO_GTFS_NAO_ENCONTRADA", "Nenhuma versão GTFS válida para a consulta.", 404)
    except LinhaNaoEncontrada as erro:
        return _erro("LINHA_NAO_ENCONTRADA", str(erro), 404)
    except VersaoGTFSAmbigua:
        return _erro("VERSAO_GTFS_AMBIGUA", "Há vigências GTFS sobrepostas; informe versao_gtfs_id.", 409)
    except ProgramacaoAmbigua as erro:
        return _erro("GRADE_AMBIGUA", str(erro), 409)
    except SQLAlchemyError as erro:
        db.session.rollback()
        # A mensagem da exceção pode conter SQL ou credenciais da conexão.
        current_app.logger.error("Falha na consulta de partidas (%s)", type(erro).__name__)
        return _erro("CONSULTA_INDISPONIVEL", "Não foi possível consultar as partidas. Tente novamente.", 503)
