"""GET /mapas/indicadores: validação da consulta espacial (US24); dados via QueryService."""

import re

from flask import Blueprint, current_app, request
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.datastructures import MultiDict

from app import db
from app.api.comum import ParametroInvalido, erro_json, limitar_por_ip
from app.api.indicadores import ler_filtros
from app.consultas.mapas import VersaoTerritorialNaoEncontrada, consultar_mapa

mapas_bp = Blueprint("mapas", __name__)

limitar_por_ip(mapas_bp, recurso="mapas",
               mensagem_excedido="Limite de consultas por minuto por IP atingido.")

_FILTROS_COMUNS = {
    "linha", "sentido", "tipo", "indicador", "versao_gtfs_id",
    "inicio", "fim", "page", "page_size",
}
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


@mapas_bp.get("/mapas/indicadores")
def consultar_mapa_indicadores():
    """Retorna FeatureCollection GeoJSON de indicadores publicados."""
    escopo = request.args.get("escopo", "").strip().lower()
    if escopo not in {"parada", "trecho", "distrito", "subprefeitura"}:
        return erro_json("PARAMETRO_INVALIDO", "escopo deve ser parada, trecho, distrito ou subprefeitura.", 400, "escopo")
    base = request.args.get("base", "").strip().lower() or None
    if escopo in {"distrito", "subprefeitura"} and base not in {"parada", "trecho"}:
        return erro_json("PARAMETRO_INVALIDO", "Informe base=parada ou base=trecho para agregar por território.", 400, "base")
    if escopo in {"parada", "trecho"} and base is not None:
        return erro_json("PARAMETRO_INVALIDO", "base só se aplica a distrito ou subprefeitura.", 400, "base")
    permitidos = _FILTROS_COMUNS | {"escopo", "base", "codigo", "versao_territorial_id"}
    for campo in request.args:
        if campo not in permitidos:
            return erro_json("PARAMETRO_INVALIDO", "Parâmetro não reconhecido.", 400, campo)
        if len(request.args.getlist(campo)) != 1:
            return erro_json("PARAMETRO_INVALIDO", "Informe cada parâmetro uma única vez.", 400, campo)
    if "codigo" in request.args and (
        not request.args["codigo"].strip()
        or len(request.args["codigo"].strip()) > 64
        or any(ord(char) < 32 or ord(char) == 127 for char in request.args["codigo"])
    ):
        return erro_json("PARAMETRO_INVALIDO", "Informe um código com até 64 caracteres.", 400, "codigo")
    versao_id = request.args.get("versao_territorial_id", "").strip() or None
    if versao_id and not _UUID.fullmatch(versao_id):
        return erro_json("PARAMETRO_INVALIDO", "Identificador territorial inválido.", 400, "versao_territorial_id")
    try:
        filtros = ler_filtros(MultiDict((k, v) for k, v in request.args.items(multi=True)
                                       if k in _FILTROS_COMUNS))
    except ParametroInvalido as erro:
        return erro_json("PARAMETRO_INVALIDO", str(erro), 400, erro.campo)
    if filtros["versao_gtfs_id"] is None:
        return erro_json("PARAMETRO_INVALIDO", "Informe versao_gtfs_id para fixar a rede e evitar misturar versões.", 400, "versao_gtfs_id")
    try:
        corpo = consultar_mapa(filtros, escopo, base=base,
                               codigo=request.args.get("codigo", "").strip() or None,
                               versao_territorial_id=versao_id)
    except VersaoTerritorialNaoEncontrada:
        return erro_json("PARAMETRO_INVALIDO", "Versão territorial não encontrada.", 400, "versao_territorial_id")
    except SQLAlchemyError as erro:
        db.session.rollback()
        current_app.logger.error("Falha na consulta de mapa (%s)", type(erro).__name__)
        return erro_json("CONSULTA_INDISPONIVEL", "Não foi possível consultar o mapa de indicadores.", 503)
    resposta = current_app.json.response(corpo)
    resposta.mimetype = "application/geo+json"
    return resposta, 200
