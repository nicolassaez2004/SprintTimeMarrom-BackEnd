"""GET /contexto/clima: chuva e temperatura de um trecho num período (US06-T04).

Só valida os parâmetros; a leitura é do QueryService (`app.consultas.clima`).
"""

import re
from datetime import datetime, timedelta

from flask import Blueprint, current_app, request
from sqlalchemy.exc import SQLAlchemyError

from app import db
from app.api.comum import erro_json, limitar_por_ip
from app.consultas.clima import VinculoClimaInexistente, consultar_clima_trecho

contexto_bp = Blueprint("contexto", __name__)

limitar_por_ip(contexto_bp, recurso="contexto",
               mensagem_excedido="Limite de consultas por minuto por IP atingido.")

_PERMITIDOS = {"trecho_id", "inicio", "fim", "fonte"}
_INSTANTE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?(Z|[+-]\d{2}:\d{2})")
PERIODO_MAXIMO = timedelta(days=31)


def _instante(campo):
    valor = request.args.get(campo, "").strip()
    if not _INSTANTE.fullmatch(valor):
        return None
    try:
        return datetime.fromisoformat(valor.replace("Z", "+00:00"))
    except ValueError:
        return None


@contexto_bp.get("/contexto/clima")
def consultar_clima():
    for campo in request.args:
        if campo not in _PERMITIDOS:
            return erro_json("PARAMETRO_INVALIDO", "Parâmetro não reconhecido.", 400, campo)
        if len(request.args.getlist(campo)) != 1:
            return erro_json("PARAMETRO_INVALIDO", "Informe cada parâmetro uma única vez.", 400, campo)
    trecho_id = request.args.get("trecho_id", "").strip()
    if not trecho_id or len(trecho_id) > 36 or any(ord(c) < 32 or ord(c) == 127 for c in trecho_id):
        return erro_json("PARAMETRO_INVALIDO", "Informe o trecho_id com até 36 caracteres.", 400, "trecho_id")
    fonte = request.args.get("fonte", "INMET").strip().upper()
    if fonte not in ("INMET", "CGE"):
        return erro_json("PARAMETRO_INVALIDO", "fonte deve ser INMET ou CGE.", 400, "fonte")
    inicio, fim = _instante("inicio"), _instante("fim")
    for campo, valor in (("inicio", inicio), ("fim", fim)):
        if valor is None:
            return erro_json("PARAMETRO_INVALIDO",
                             "Informe data e hora ISO 8601 com fuso, por exemplo 2026-11-23T12:00:00Z.",
                             400, campo)
    if fim <= inicio or fim - inicio > PERIODO_MAXIMO:
        return erro_json("PARAMETRO_INVALIDO", "Informe fim depois de inicio, com até 31 dias entre eles.",
                         400, "fim")
    try:
        corpo = consultar_clima_trecho(trecho_id, inicio, fim, fonte=fonte)
    except VinculoClimaInexistente:
        return erro_json("NAO_ENCONTRADO",
                         "Trecho sem vínculo de clima calculado para esta fonte.", 404, "trecho_id")
    except SQLAlchemyError as erro:
        db.session.rollback()
        current_app.logger.error("Falha na consulta de clima (%s)", type(erro).__name__)
        return erro_json("CONSULTA_INDISPONIVEL", "Não foi possível consultar o clima do trecho.", 503)
    return corpo, 200
