"""Contrato público de indicadores e séries (US11-T01).

As rotas são deliberadamente públicas. Enquanto o conjunto publicado da
US14/US23 não existir, respondem com 503 em vez de apresentar dados brutos
como se fossem indicadores calculados.
"""

import re
from datetime import datetime
from pathlib import Path

from flask import Blueprint, current_app, g, request, send_file
from sqlalchemy.exc import SQLAlchemyError

from app import db
from app.core.limites import IdentidadeIPInvalida, consumir_limite, identificar_ip
from app.indicadores.service import consultar_indicadores

indicadores_bp = Blueprint("indicadores", __name__)


@indicadores_bp.before_request
def limitar_consulta_publica():
    if request.method not in ("GET", "HEAD") or request.endpoint not in (
        "indicadores.indicadores", "indicadores.series",
    ):
        return None
    try:
        consumo = consumir_limite(identificar_ip(request))
    except IdentidadeIPInvalida:
        return _erro("IP_NAO_IDENTIFICADO", "Não foi possível identificar o IP da conexão ou do proxy.", 400)
    except SQLAlchemyError as erro:
        current_app.logger.error("Limitador de indicadores indisponível (%s)", type(erro).__name__)
        resposta, status = _erro("LIMITE_INDISPONIVEL", "Não foi possível verificar o limite. Tente novamente em instantes.", 503)
        return resposta, status, {"Retry-After": "5", "Cache-Control": "no-store"}
    g.limite_indicadores = consumo
    if not consumo.permitido:
        return {"erro": {"codigo": "LIMITE_EXCEDIDO",
            "mensagem": "Limite de 60 consultas por minuto por IP atingido. Aguarde e tente novamente.",
            "tentar_em_segundos": consumo.tentar_em}}, 429, {
                "Retry-After": str(consumo.tentar_em), "Cache-Control": "no-store"}
    return None


@indicadores_bp.after_request
def informar_limite(resposta):
    consumo = g.get("limite_indicadores")
    if consumo is not None:
        resposta.headers.update(consumo.cabecalhos())
        resposta.headers["Cache-Control"] = "no-store"
    return resposta

_PARAMETROS = {
    "linha", "sentido", "tipo", "indicador", "parada", "trecho",
    "versao_gtfs_id", "inicio", "fim", "page", "page_size",
}
_INSTANTE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")
_CODIGO = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
PAGE_SIZE_MAX = 100


class ParametroInvalido(ValueError):
    def __init__(self, campo, mensagem):
        self.campo = campo
        super().__init__(mensagem)


def _ler_instante(args, campo):
    valor = args[campo].strip()
    if not _INSTANTE.fullmatch(valor):
        raise ParametroInvalido(campo, "Informe data e hora ISO 8601 com fuso, por exemplo 2026-11-23T12:00:00Z.")
    try:
        instante = datetime.fromisoformat(valor.replace("Z", "+00:00"))
    except ValueError as erro:
        raise ParametroInvalido(campo, "Informe um instante ISO 8601 válido.") from erro
    return instante


def ler_filtros(args):
    for campo in args:
        if campo not in _PARAMETROS:
            raise ParametroInvalido(campo, "Parâmetro não reconhecido.")
        if len(args.getlist(campo)) != 1:
            raise ParametroInvalido(campo, "Informe cada parâmetro uma única vez.")

    linha = args.get("linha", "").strip() or None
    if linha is not None and (len(linha) > 64 or any(ord(c) < 32 or ord(c) == 127 for c in linha)):
        raise ParametroInvalido("linha", "Informe um código de linha com até 64 caracteres, sem controles.")
    if "tipo" in args and "indicador" in args:
        raise ParametroInvalido("tipo", "Use tipo ou indicador, não ambos.")
    tipo = (args.get("tipo") or args.get("indicador") or "").strip() or None
    if tipo is not None and not _CODIGO.fullmatch(tipo):
        raise ParametroInvalido("tipo", "Use de 1 a 64 letras, números ou os caracteres _ . : -.")
    sentido = args.get("sentido", "").strip().lower() or None
    if sentido in ("ida", "volta"):
        sentido = "0" if sentido == "ida" else "1"
    if sentido is not None and sentido not in ("0", "1"):
        raise ParametroInvalido("sentido", "Informe 0/ida ou 1/volta.")

    parada = args.get("parada", "").strip() or None
    trecho = args.get("trecho", "").strip() or None
    for campo, valor in (("parada", parada), ("trecho", trecho)):
        if valor is not None and (len(valor) > 64 or any(ord(c) < 32 or ord(c) == 127 for c in valor)):
            raise ParametroInvalido(campo, "Informe um código com até 64 caracteres, sem controles.")
    if parada is not None and trecho is not None:
        raise ParametroInvalido("trecho", "Filtre por parada ou trecho, não pelos dois simultaneamente.")

    versao_gtfs_id = args.get("versao_gtfs_id", "").strip() or None
    if versao_gtfs_id is not None and not re.fullmatch(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
        versao_gtfs_id,
    ):
        raise ParametroInvalido("versao_gtfs_id", "Informe um UUID GTFS com hífens.")

    inicio = _ler_instante(args, "inicio") if "inicio" in args else None
    fim = _ler_instante(args, "fim") if "fim" in args else None
    if inicio and fim and fim < inicio:
        raise ParametroInvalido("fim", "fim deve ser igual ou posterior a inicio.")

    def inteiro(campo, padrao, minimo, maximo):
        valor = args.get(campo, str(padrao)).strip()
        if not re.fullmatch(r"\d+", valor):
            raise ParametroInvalido(campo, "Informe um número inteiro positivo.")
        numero = int(valor)
        if not minimo <= numero <= maximo:
            raise ParametroInvalido(campo, f"Informe um valor entre {minimo} e {maximo}.")
        return numero

    return {
        "linha": linha,
        "sentido": sentido,
        "tipo": tipo,
        "parada": parada,
        "trecho": trecho,
        "versao_gtfs_id": versao_gtfs_id.lower() if versao_gtfs_id else None,
        "inicio": inicio,
        "fim": fim,
        "page": inteiro("page", 1, 1, 2_147_483_647),
        "page_size": inteiro("page_size", 100, 1, PAGE_SIZE_MAX),
    }


def _erro(codigo, mensagem, status, campo=None):
    detalhe = {"codigo": codigo, "mensagem": mensagem}
    if campo is not None:
        detalhe["campo"] = campo
    return {"erro": detalhe}, status


def _responder(*, serie=False):
    try:
        filtros = ler_filtros(request.args)
    except ParametroInvalido as erro:
        return _erro("PARAMETRO_INVALIDO", str(erro), 400, erro.campo)
    try:
        return consultar_indicadores(filtros, serie=serie), 200
    except SQLAlchemyError as erro:
        db.session.rollback()
        current_app.logger.error("Falha ao consultar indicadores (%s)", type(erro).__name__)
        return _erro("CONSULTA_INDISPONIVEL", "Não foi possível consultar os indicadores. Tente novamente.", 503)


@indicadores_bp.get("/indicadores")
def indicadores():
    """Consulta indicadores operacionais publicados."""
    return _responder()


@indicadores_bp.get("/series")
def series():
    """Consulta séries temporais de indicadores publicados."""
    return _responder(serie=True)


_DOCUMENTACAO = Path(__file__).resolve().parents[2] / "docs"


@indicadores_bp.get("/docs/indicadores")
def documentacao_publica():
    """Guia público; não consome o orçamento das consultas de indicadores."""
    return send_file(_DOCUMENTACAO / "indicadores.html", mimetype="text/html", max_age=0)


@indicadores_bp.get("/docs/indicadores/openapi.yaml")
def contrato_publico():
    resposta = send_file(_DOCUMENTACAO / "openapi.yaml", mimetype="application/yaml", max_age=0)
    resposta.headers["X-Content-Type-Options"] = "nosniff"
    return resposta


@indicadores_bp.get("/docs/indicadores/ensaio.json")
def evidencia_publica():
    resposta = send_file(_DOCUMENTACAO / "evidencias/US11-T03-desempenho.json",
                         mimetype="application/json", max_age=0)
    resposta.headers["X-Content-Type-Options"] = "nosniff"
    return resposta
