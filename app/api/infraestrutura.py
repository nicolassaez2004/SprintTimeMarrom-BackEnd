"""Detalhamento da associação de infraestrutura de um trecho."""
from datetime import date

from flask import Blueprint, request
from sqlalchemy.exc import SQLAlchemyError

from app import db
from app.api.comum import erro_json, limitar_por_ip
from app.contexto.infraestrutura.associacao import detalhar_trecho

infraestrutura_bp = Blueprint("infraestrutura", __name__)
limitar_por_ip(infraestrutura_bp, recurso="infraestrutura",
               mensagem_excedido="Limite de consultas por minuto por IP atingido.")


@infraestrutura_bp.get("/trechos/<trecho_id>/infraestrutura")
def obter_infraestrutura_trecho(trecho_id):
    data_texto = request.args.get("data", "").strip()
    if not data_texto or len(request.args) != 1:
        return erro_json("PARAMETRO_INVALIDO", "Informe somente data=AAAA-MM-DD.", 400, "data")
    try:
        quando = date.fromisoformat(data_texto)
    except ValueError:
        return erro_json("PARAMETRO_INVALIDO", "Data deve seguir AAAA-MM-DD.", 400, "data")
    try:
        resultado = detalhar_trecho(trecho_id, quando=quando)
    except SQLAlchemyError:
        db.session.rollback()
        return erro_json("CONSULTA_INDISPONIVEL", "Não foi possível consultar a infraestrutura.", 503)
    if resultado is None:
        return erro_json("TRECHO_NAO_ENCONTRADO", "Trecho não encontrado.", 404, "trecho_id")
    return resultado, 200
