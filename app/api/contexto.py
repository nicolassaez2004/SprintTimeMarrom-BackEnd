"""Contexto por trecho num período: GET /contexto/clima (US06-T04) e
GET /contexto/trafego (US08-T03).

Só validam os parâmetros; a leitura é do QueryService (`app.consultas.clima`,
`app.consultas.trafego`). Públicas, sem login, com o limite por IP da US11.
"""

import re
from datetime import datetime, timedelta

from flask import Blueprint, current_app, request
from sqlalchemy.exc import SQLAlchemyError

from app import db
from app.api.comum import erro_json, limitar_por_ip
from app.consultas.clima import VinculoClimaInexistente, consultar_clima_trecho
from app.consultas.trafego import FONTE_AGREGADO, TrechoInexistente, consultar_trafego_trecho

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


class _ParametroInvalido(Exception):
    def __init__(self, campo, mensagem):
        super().__init__(mensagem)
        self.resposta = erro_json("PARAMETRO_INVALIDO", mensagem, 400, campo)


def _validar(permitidos):
    """Parâmetros comuns: ``(trecho_id, inicio, fim)``; levanta ``_ParametroInvalido``."""
    for campo in request.args:
        if campo not in permitidos:
            raise _ParametroInvalido(campo, "Parâmetro não reconhecido.")
        if len(request.args.getlist(campo)) != 1:
            raise _ParametroInvalido(campo, "Informe cada parâmetro uma única vez.")
    trecho_id = request.args.get("trecho_id", "").strip()
    if not trecho_id or len(trecho_id) > 36 or any(ord(c) < 32 or ord(c) == 127 for c in trecho_id):
        raise _ParametroInvalido("trecho_id", "Informe o trecho_id com até 36 caracteres.")
    inicio, fim = _instante("inicio"), _instante("fim")
    for campo, valor in (("inicio", inicio), ("fim", fim)):
        if valor is None:
            raise _ParametroInvalido(
                campo, "Informe data e hora ISO 8601 com fuso, por exemplo 2026-11-23T12:00:00Z.")
    if fim <= inicio or fim - inicio > PERIODO_MAXIMO:
        raise _ParametroInvalido("fim", "Informe fim depois de inicio, com até 31 dias entre eles.")
    return trecho_id, inicio, fim


@contexto_bp.errorhandler(_ParametroInvalido)
def _parametro_invalido(erro):
    return erro.resposta


@contexto_bp.get("/contexto/clima")
def consultar_clima():
    trecho_id, inicio, fim = _validar(_PERMITIDOS)
    fonte = request.args.get("fonte", "INMET").strip().upper()
    if fonte not in ("INMET", "CGE"):
        return erro_json("PARAMETRO_INVALIDO", "fonte deve ser INMET ou CGE.", 400, "fonte")
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


_PERMITIDOS_TRAFEGO = {"trecho_id", "inicio", "fim", "versao_fonte", "fonte"}


@contexto_bp.get("/contexto/trafego")
def consultar_trafego():
    trecho_id, inicio, fim = _validar(_PERMITIDOS_TRAFEGO)
    fonte = request.args.get("fonte", FONTE_AGREGADO).strip().upper()
    if fonte != FONTE_AGREGADO:
        # Política de fontes: sem acordo, o Waze não é integrado (docs/US08-T03.md).
        raise _ParametroInvalido(
            "fonte", f"Única fonte disponível: {FONTE_AGREGADO} (proxy pela velocidade dos ônibus).")
    versao_fonte = request.args.get("versao_fonte", "").strip() or None
    if versao_fonte and (len(versao_fonte) > 128 or any(ord(c) < 32 or ord(c) == 127 for c in versao_fonte)):
        raise _ParametroInvalido("versao_fonte", "versao_fonte deve ter até 128 caracteres.")
    try:
        corpo = consultar_trafego_trecho(trecho_id, inicio, fim, versao_fonte=versao_fonte)
    except TrechoInexistente:
        return erro_json("NAO_ENCONTRADO", "Trecho inexistente.", 404, "trecho_id")
    except SQLAlchemyError as erro:
        db.session.rollback()
        current_app.logger.error("Falha na consulta de tráfego (%s)", type(erro).__name__)
        return erro_json("CONSULTA_INDISPONIVEL", "Não foi possível consultar o tráfego do trecho.", 503)
    return corpo, 200
