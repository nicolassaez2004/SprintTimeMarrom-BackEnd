"""Consulta pública das taxas e incidentes de saúde (US30)."""
from flask import Blueprint, current_app, request
from sqlalchemy.exc import SQLAlchemyError

from app import db
from app.api.comum import erro_json, limitar_por_ip
from app.core.tempo import FUSO
from app.models import agora_utc
from app.monitoramento.saude import consultar_saude

saude_bp = Blueprint("saude", __name__)
limitar_por_ip(saude_bp, recurso="saude", mensagem_excedido="Limite de consultas por minuto por IP atingido.")


@saude_bp.get("/saude")
@saude_bp.get("/api/saude")
def saude():
    for campo in request.args:
        if campo != "mes":
            return erro_json("PARAMETRO_INVALIDO", "Parâmetro não reconhecido.", 400, campo)
        if len(request.args.getlist(campo)) != 1:
            return erro_json("PARAMETRO_INVALIDO", "Informe cada parâmetro uma única vez.", 400, campo)
    agora = agora_utc()
    mes = request.args.get("mes", agora.astimezone(FUSO).strftime("%Y-%m"))
    try:
        return consultar_saude(mes, agora=agora), 200
    except ValueError:
        return erro_json("PARAMETRO_INVALIDO", "Informe um mês válido, não futuro, no formato AAAA-MM.", 400, "mes")
    except SQLAlchemyError as erro:
        db.session.rollback()
        current_app.logger.error("Consulta de saúde indisponível (%s)", type(erro).__name__)
        return erro_json("CONSULTA_INDISPONIVEL", "Não foi possível consultar a saúde.", 503)
