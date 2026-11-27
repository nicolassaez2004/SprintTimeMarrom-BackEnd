"""US28: POST /extracoes registra o pedido e responde 202; GET /extracoes/<id> acompanha.

As consultas só leem o MySQL e o FileStore: nunca iniciam jobs nem geram arquivos.
"""
import re

from flask import Blueprint, current_app, request, send_file
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


def _pedido(servico, pedido_id):
    """Id opaco: formato fora do UUID é tratado como inexistente (404), sem consultar."""
    return servico.obter(pedido_id) if _UUID.fullmatch(pedido_id) else None


def _nao_encontrado():
    return erro_json("PEDIDO_NAO_ENCONTRADO", "Pedido de extração não encontrado.", 404)


@extracoes_bp.get("/extracoes/<pedido_id>")
@extracoes_bp.get("/api/extracoes/<pedido_id>")
def consultar_extracao(pedido_id):
    servico = ExportService()
    try:
        pedido = _pedido(servico, pedido_id)
        if pedido is None:
            return _nao_encontrado()
        return servico.descrever(pedido), 200, _SEM_CACHE
    except SQLAlchemyError as falha:
        return _indisponivel(falha)


@extracoes_bp.get("/extracoes/<pedido_id>/arquivo")
@extracoes_bp.get("/api/extracoes/<pedido_id>/arquivo")
def baixar_extracao(pedido_id):
    servico = ExportService()
    try:
        pedido = _pedido(servico, pedido_id)
        if pedido is None:
            return _nao_encontrado()
        estado, _ = servico.estado(pedido)
        if estado != "pronto":
            resposta, status = erro_json("EXTRACAO_NAO_PRONTA",
                                         "O arquivo só pode ser baixado quando a extração estiver pronta.",
                                         409, estado=estado)
            return resposta, status, _SEM_CACHE
        arquivo = pedido.arquivo
        caminho = servico.caminho_publicado(pedido)
    except SQLAlchemyError as falha:
        return _indisponivel(falha)
    except FileNotFoundError:
        current_app.logger.error("Extração %s: arquivo final divergente do hash", pedido_id)
        return erro_json("ARQUIVO_INDISPONIVEL", "O arquivo final não está disponível.", 404)
    return send_file(caminho, mimetype=arquivo.mime_type, as_attachment=True,
                     download_name=servico.nome_arquivo(pedido), etag=arquivo.sha256, max_age=0)
