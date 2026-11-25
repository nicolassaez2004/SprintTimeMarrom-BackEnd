"""Consulta pública do acervo e entrega do PDF original (US05-T02). Somente leitura."""

import re
from datetime import date

from flask import Blueprint, current_app, request, send_file
from sqlalchemy.exc import SQLAlchemyError

from app import db
from app.documentos.service import buscar_documentos, caminho_original, consultar_documento

documentos_bp = Blueprint("documentos", __name__)

PARAMETROS = ("tipo", "data_inicio", "data_fim", "pagina", "por_pagina")
POR_PAGINA_PADRAO, POR_PAGINA_MAX = 20, 100


class ParametroInvalido(ValueError):
    def __init__(self, campo, mensagem):
        self.campo = campo
        super().__init__(mensagem)


def _erro(codigo, mensagem, status, campo=None):
    detalhe = {"codigo": codigo, "mensagem": mensagem}
    if campo is not None:
        detalhe["campo"] = campo
    return {"erro": detalhe}, status


def _data(valor, campo):
    try:
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", valor):
            raise ValueError()
        return date.fromisoformat(valor)
    except ValueError as erro:
        raise ParametroInvalido(campo, "Informe uma data válida no formato YYYY-MM-DD.") from erro


def _inteiro(valor, campo, minimo, maximo):
    if not re.fullmatch(r"[0-9]{1,6}", valor) or not minimo <= int(valor) <= maximo:
        raise ParametroInvalido(campo, f"Informe um inteiro entre {minimo} e {maximo}.")
    return int(valor)


def ler_parametros(parametros):
    for campo in parametros:
        if campo not in PARAMETROS:
            raise ParametroInvalido(campo, "Parâmetro não reconhecido.")
        if len(parametros.getlist(campo)) != 1:
            raise ParametroInvalido(campo, "Informe cada parâmetro uma única vez.")
    valores = {campo: parametros[campo].strip() for campo in parametros}
    filtros = {"pagina": 1, "por_pagina": POR_PAGINA_PADRAO}
    if "tipo" in valores:
        if not valores["tipo"] or len(valores["tipo"]) > 64:
            raise ParametroInvalido("tipo", "Informe um tipo não vazio com até 64 caracteres.")
        filtros["tipo"] = valores["tipo"]
    for campo in ("data_inicio", "data_fim"):
        if campo in valores:
            filtros[campo] = _data(valores[campo], campo)
    if "data_inicio" in filtros and "data_fim" in filtros and filtros["data_fim"] < filtros["data_inicio"]:
        raise ParametroInvalido("data_fim", "data_fim deve ser igual ou posterior a data_inicio.")
    if "pagina" in valores:
        filtros["pagina"] = _inteiro(valores["pagina"], "pagina", 1, 100000)
    if "por_pagina" in valores:
        filtros["por_pagina"] = _inteiro(valores["por_pagina"], "por_pagina", 1, POR_PAGINA_MAX)
    return filtros


def _falha_banco(falha):
    db.session.rollback()
    # A mensagem da exceção pode conter SQL ou credenciais da conexão.
    current_app.logger.error("Falha na consulta de documentos (%s)", type(falha).__name__)
    return _erro("CONSULTA_INDISPONIVEL", "Não foi possível consultar o acervo. Tente novamente.", 503)


@documentos_bp.get("/documentos")
@documentos_bp.get("/api/documentos")
def listar_documentos():
    try:
        filtros = ler_parametros(request.args)
    except ParametroInvalido as erro:
        return _erro("PARAMETRO_INVALIDO", str(erro), 400, erro.campo)
    try:
        resultado = buscar_documentos(**filtros)
    except SQLAlchemyError as falha:
        return _falha_banco(falha)
    resultado["filtros"] = {campo: (valor.isoformat() if isinstance(valor, date) else valor)
                            for campo, valor in filtros.items()
                            if campo in ("tipo", "data_inicio", "data_fim")}
    return resultado, 200


@documentos_bp.get("/documentos/<documento_id>/arquivo")
@documentos_bp.get("/api/documentos/<documento_id>/arquivo")
def baixar_arquivo(documento_id):
    try:
        documento = consultar_documento(documento_id)
        caminho = caminho_original(documento_id)
    except LookupError:
        return _erro("DOCUMENTO_NAO_ENCONTRADO", "Documento oficial não catalogado.", 404)
    except FileNotFoundError:
        current_app.logger.error("PDF do documento %s ausente ou divergente do hash", documento_id)
        return _erro("ARQUIVO_INDISPONIVEL",
                     "O PDF original deste documento está indisponível no momento.", 503)
    except SQLAlchemyError as falha:
        return _falha_banco(falha)
    arquivo = documento["arquivo"]
    resposta = send_file(caminho, mimetype="application/pdf", download_name=arquivo["nome_original"],
                         etag=arquivo["sha256"], max_age=0)
    resposta.headers["X-Content-Type-Options"] = "nosniff"
    resposta.headers["X-Conteudo-SHA256"] = arquivo["sha256"]
    return resposta
