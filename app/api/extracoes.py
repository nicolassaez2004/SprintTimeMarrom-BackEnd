"""US28: POST /extracoes registra o pedido e responde 202; GET /extracoes/<id> acompanha."""
import re

from flask import Blueprint, current_app, request
from sqlalchemy.exc import SQLAlchemyError

from app import db
from app.api.comum import erro_json
from app.exportacoes.extracoes import ExportService, PedidoInvalido

extracoes_bp = Blueprint("extracoes", __name__)
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_SEM_CACHE = {"Cache-Control": "no-store"}


def _indisponivel(falha):
    db.session.rollback()
    current_app.logger.error("Extrações indisponíveis (%s)", type(falha).__name__)
    resposta, status = erro_json("EXTRACOES_INDISPONIVEIS",
                                 "Não foi possível registrar ou consultar o pedido. Tente novamente.", 503)
    return resposta, status, {**_SEM_CACHE, "Retry-After": "5"}


@extracoes_bp.post("/extracoes")
@extracoes_bp.post("/api/extracoes")
def solicitar_extracao():
    servico = ExportService()
    try:
        pedido = servico.solicitar(request.get_json(silent=True))
        corpo = servico.descrever(pedido)
    except PedidoInvalido as invalido:
        return erro_json("PARAMETRO_INVALIDO", str(invalido), 400, invalido.campo)
    except SQLAlchemyError as falha:
        return _indisponivel(falha)
    return corpo, 202, {**_SEM_CACHE, "Location": corpo["consulta"]}


@extracoes_bp.get("/extracoes/<pedido_id>")
@extracoes_bp.get("/api/extracoes/<pedido_id>")
def consultar_extracao(pedido_id):
    servico = ExportService()
    try:
        pedido = servico.obter(pedido_id) if _UUID.fullmatch(pedido_id) else None
        if pedido is None:
            return erro_json("PEDIDO_NAO_ENCONTRADO", "Pedido de extração não encontrado.", 404)
        return servico.descrever(pedido), 200, _SEM_CACHE
    except SQLAlchemyError as falha:
        return _indisponivel(falha)
