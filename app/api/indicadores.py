"""Consulta pública dos indicadores persistidos (US11/US23)."""

import re
from datetime import datetime, time, timedelta
from pathlib import Path

from flask import Blueprint, current_app, request, send_file
from sqlalchemy.exc import SQLAlchemyError

from app import db
from app.api.comum import ParametroInvalido, erro_json, limitar_por_ip
from app.indicadores.frequencia import AGREGACOES, TIPO_PUBLICO, consultar_frequencias
from app.indicadores.regularidade import REGULARIDADE, consultar_regularidade
from app.indicadores.espera import TIPO_PUBLICO as ESPERA, consultar_espera
from app.indicadores.service import consultar_indicadores
from app.indicadores.series import consultar_series
from app.indicadores.agenda import estado_atualizacao, marcar_consolidados, versao_vigente
from app.core.tempo import FUSO

indicadores_bp = Blueprint("indicadores", __name__)


limitar_por_ip(
    indicadores_bp, recurso="indicadores",
    endpoints=("indicadores.indicadores", "indicadores.series"),
    mensagem_excedido="Limite de 60 consultas por minuto por IP atingido. Aguarde e tente novamente.",
)

_PARAMETROS = {
    "linha", "sentido", "tipo", "indicador", "parada", "trecho",
    "versao_gtfs_id", "inicio", "fim", "page", "page_size",
}
_INSTANTE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")
_CODIGO = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
PAGE_SIZE_MAX = 100


def _ler_instante(args, campo):
    valor = args[campo].strip()
    if not _INSTANTE.fullmatch(valor):
        raise ParametroInvalido(campo, "Informe data e hora ISO 8601 com fuso, por exemplo 2026-11-23T12:00:00Z.")
    try:
        instante = datetime.fromisoformat(valor.replace("Z", "+00:00"))
    except ValueError as erro:
        raise ParametroInvalido(campo, "Informe um instante ISO 8601 válido.") from erro
    return instante


def ler_filtros(args, *, serie=False):
    # Frequência (US15-T03), regularidade (US17-T03A) e espera (US18-T03A) aceitam faixa e agregação.
    frequencia = not serie and (args.get("tipo") or args.get("indicador") or "").strip() in (
        TIPO_PUBLICO, REGULARIDADE, ESPERA)
    permitidos = (_PARAMETROS | ({"hora_inicio", "hora_fim"} if serie or frequencia else set())
                  | ({"agregacao"} if frequencia else set()))
    for campo in args:
        if campo not in permitidos:
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

    filtros = {
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
    if serie or frequencia:
        horas = []
        for campo in ("hora_inicio", "hora_fim"):
            valor = args.get(campo)
            if valor is not None and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", valor):
                raise ParametroInvalido(campo, "Informe a hora no formato HH:MM, de 00:00 a 23:59.")
            horas.append(valor)
        if (horas[0] is None) != (horas[1] is None):
            raise ParametroInvalido("hora_inicio", "Informe hora_inicio e hora_fim juntas.")
        if horas[0] is not None and horas[0] == horas[1]:
            raise ParametroInvalido("hora_fim", "Use horas diferentes; omita a faixa para consultar o dia inteiro.")
        if serie and inicio and fim and fim == inicio:
            raise ParametroInvalido("fim", "fim deve ser posterior a inicio na consulta de séries.")
        filtros.update(hora_inicio=horas[0], hora_fim=horas[1])
    if frequencia:
        filtros["agregacao"] = _ler_agregacao(args, filtros)
    return filtros


def _ler_agregacao(args, filtros):
    """US15-T03: limites da consulta de frequência (lida em memória, por linha)."""
    agregacao = args.get("agregacao", "janela").strip()
    inicio, fim, faixa = filtros["inicio"], filtros["fim"], filtros["hora_inicio"] is not None
    if agregacao not in AGREGACOES:
        raise ParametroInvalido("agregacao", "Use janela, faixa, dia ou mes.")
    if filtros["linha"] is None:
        raise ParametroInvalido("linha", "Informe a linha para consultar frequência, regularidade ou espera.")
    if filtros["trecho"] is not None:
        raise ParametroInvalido("trecho", "Frequência, regularidade e espera são publicadas por parada, não por trecho.")
    if inicio is None or fim is None or fim - inicio > timedelta(days=31):
        raise ParametroInvalido("inicio", "Informe inicio e fim, com até 31 dias entre eles.")
    if agregacao != "janela" and fim == inicio:
        raise ParametroInvalido("fim", "Nas agregações, fim deve ser posterior a inicio.")
    if agregacao == "faixa" and not faixa:
        raise ParametroInvalido("hora_inicio", "agregacao=faixa exige hora_inicio e hora_fim.")
    if faixa and agregacao in ("janela", "dia"):
        raise ParametroInvalido("hora_inicio", "Use hora_inicio/hora_fim com agregacao=faixa ou mes.")
    if agregacao != "janela" and any(i.astimezone(FUSO).time() != time() for i in (inicio, fim)):
        raise ParametroInvalido("inicio", "Nas agregações, inicio e fim devem ser meia-noite de America/Sao_Paulo.")
    return agregacao


def _responder(*, serie=False):
    try:
        filtros = ler_filtros(request.args, serie=serie)
    except ParametroInvalido as erro:
        return erro_json("PARAMETRO_INVALIDO", str(erro), 400, erro.campo)
    try:
        if serie:
            # US23-T03: estado das agendas lido na mesma transação dos dados.
            corpo = consultar_series(filtros)
            corpo["atualizacao"] = estado_atualizacao()
            marcar_consolidados(corpo["dados"], corpo["atualizacao"])
            # US25-T01: versão a enviar em GET /downloads/indicadores?versao_dados=...
            corpo["versao_dados"] = versao_vigente()
            return corpo, 200
        if filtros.get("agregacao") and filtros["tipo"] == REGULARIDADE:
            corpo = consultar_regularidade(filtros)
        elif filtros.get("agregacao") and filtros["tipo"] == ESPERA:
            corpo = consultar_espera(filtros)
        elif filtros.get("agregacao"):
            corpo = consultar_frequencias(filtros)
        else:
            corpo = consultar_indicadores(filtros)
        # US23-T03: o mesmo estado de atualização/consolidação de /series (provisório × consolidado).
        corpo["atualizacao"] = estado_atualizacao()
        marcar_consolidados(corpo["dados"], corpo["atualizacao"])
        corpo["versao_dados"] = versao_vigente()
        return corpo, 200
    except SQLAlchemyError as erro:
        db.session.rollback()
        current_app.logger.error("Falha ao consultar indicadores (%s)", type(erro).__name__)
        return erro_json("CONSULTA_INDISPONIVEL", "Não foi possível consultar os indicadores. Tente novamente.", 503)


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
