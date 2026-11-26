"""GET /atrasos/ranking (US19-T03): até 20 trechos com maior aumento de atraso no mês.

Só valida os parâmetros e lê o agregado já publicado (``app.consultas.atrasos``).
Pública, sem login, com o limite por IP da US11.
"""

import re

from flask import Blueprint, current_app, request
from sqlalchemy.exc import SQLAlchemyError

from app import db
from app.api.comum import erro_json, limitar_por_ip
from app.consultas.atrasos import ranking_mensal
from app.indicadores.propagacao import METRICAS_MENSAIS, limites_mes, metrica_configurada

atrasos_bp = Blueprint("atrasos", __name__)

limitar_por_ip(atrasos_bp, recurso="atrasos",
               mensagem_excedido="Limite de consultas por minuto por IP atingido.")

_PERMITIDOS = {"mes", "linha", "sentido", "versao_gtfs_id"}
_CODIGO = re.compile(r"[A-Za-z0-9_.:-]{1,64}")


@atrasos_bp.get("/atrasos/ranking")
@atrasos_bp.get("/api/atrasos/ranking")
def ranking():
    for campo in request.args:
        if campo not in _PERMITIDOS:
            return erro_json("PARAMETRO_INVALIDO", "Parâmetro não reconhecido.", 400, campo)
        if len(request.args.getlist(campo)) != 1:
            return erro_json("PARAMETRO_INVALIDO", "Informe cada parâmetro uma única vez.", 400, campo)
    mes = request.args.get("mes", "").strip()
    try:
        limites_mes(mes)
    except ValueError:
        return erro_json("PARAMETRO_INVALIDO", "Informe mes no formato AAAA-MM, por exemplo 2026-11.",
                         400, "mes")
    filtros = {campo: request.args[campo].strip() for campo in ("linha", "versao_gtfs_id", "sentido")
               if campo in request.args}
    for campo in ("linha", "versao_gtfs_id"):
        if campo in filtros and not _CODIGO.fullmatch(filtros[campo]):
            return erro_json("PARAMETRO_INVALIDO", f"{campo} inválido.", 400, campo)
    if filtros.get("sentido", "0") not in ("0", "1"):
        return erro_json("PARAMETRO_INVALIDO", "sentido deve ser 0 (ida) ou 1 (volta).", 400, "sentido")
    metrica = metrica_configurada()
    if metrica is None:
        return erro_json("METRICA_PENDENTE", "Métrica mensal do aumento de atraso ainda não aprovada "
                         "pelo PO; ranking não publicado.", 409, candidatas=sorted(METRICAS_MENSAIS))
    try:
        return ranking_mensal(mes, metrica, **filtros), 200
    except SQLAlchemyError as erro:
        db.session.rollback()
        current_app.logger.error("Falha na consulta do ranking de atrasos (%s)", type(erro).__name__)
        return erro_json("CONSULTA_INDISPONIVEL", "Não foi possível consultar o ranking.", 503)
