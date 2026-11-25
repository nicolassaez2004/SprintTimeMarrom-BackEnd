"""GET /qualidade: relatório público de qualidade da coleta (US13-T02)."""

import re
from datetime import date, timedelta

from flask import Blueprint, current_app, request
from sqlalchemy.exc import SQLAlchemyError

from app import db
from app.api.comum import ParametroInvalido, erro_json
from app.models import agora_utc
from app.core.tempo import FUSO
from app.qualidade.service import consolidar_qualidade

qualidade_coleta_bp = Blueprint("qualidade_coleta", __name__)

MAX_DIAS = 31


def _data(parametros, campo):
    valor = parametros.get(campo, "").strip()
    try:
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", valor):
            raise ValueError()
        return date.fromisoformat(valor)
    except ValueError as erro:
        raise ParametroInvalido(campo, "Informe uma data válida no formato YYYY-MM-DD.") from erro


def ler_parametros(parametros, hoje):
    for campo in parametros:
        if campo not in ("inicio", "fim", "janelas"):
            raise ParametroInvalido(campo, "Parâmetro não reconhecido.")
        if len(parametros.getlist(campo)) != 1:
            raise ParametroInvalido(campo, "Informe cada parâmetro uma única vez.")
    if "inicio" not in parametros:
        raise ParametroInvalido("inicio", "Parâmetro obrigatório.")
    inicio = _data(parametros, "inicio")
    fim = _data(parametros, "fim") if "fim" in parametros else inicio
    if fim < inicio:
        raise ParametroInvalido("fim", "fim deve ser igual ou posterior a inicio.")
    if fim > hoje:
        raise ParametroInvalido("fim", "Não há qualidade de coleta para datas futuras.")
    if fim - inicio >= timedelta(days=MAX_DIAS):
        raise ParametroInvalido("fim", f"Consulte no máximo {MAX_DIAS} dias por vez.")
    janelas = parametros.get("janelas", "false").strip().lower()
    if janelas not in ("true", "false"):
        raise ParametroInvalido("janelas", "Informe true ou false.")
    return inicio, fim, janelas == "true"


@qualidade_coleta_bp.get("/qualidade")
@qualidade_coleta_bp.get("/api/qualidade")
def qualidade():
    agora = agora_utc()
    try:
        inicio, fim, janelas = ler_parametros(request.args, agora.astimezone(FUSO).date())
    except ParametroInvalido as erro:
        return erro_json("PARAMETRO_INVALIDO", str(erro), 400, erro.campo)
    try:
        return consolidar_qualidade(inicio, fim, agora=agora, incluir_janelas=janelas), 200
    except SQLAlchemyError as falha:
        db.session.rollback()
        # A mensagem da exceção pode conter SQL ou credenciais da conexão.
        current_app.logger.error("Falha na consulta de qualidade (%s)", type(falha).__name__)
        return erro_json("CONSULTA_INDISPONIVEL",
                     "Não foi possível consultar a qualidade. Tente novamente.", 503)
