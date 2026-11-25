"""Downloads públicos dos brutos diários em Parquet (US09-T02). Somente leitura."""

import csv
import io
import re
from datetime import date, datetime

from flask import Blueprint, current_app, request, send_file
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.datastructures import MultiDict

from app import db
from app.exportacoes.csv import (
    FONTE as FONTE_INDICADORES,
    FONTE_URL as FONTE_INDICADORES_URL,
    LICENCA as LICENCA_INDICADORES,
    LICENCA_URL as LICENCA_INDICADORES_URL,
    CAMPOS as CAMPOS_INDICADORES,
    arquivo_indicadores_completo,
    disponibilidade_csv,
)
from app.exportacoes.parquet import (
    FONTE, FUSO, LICENCA, MIME_PARQUET, arquivo_completo, datas_disponiveis,
    diretorio_exportacoes, dicionario_dados, nome_arquivo, prazo_publicacao, periodo_utc,
)
from app.indicadores.routes import ParametroInvalido, ler_filtros
from app.models import agora_utc

downloads_bp = Blueprint("downloads", __name__)


def _erro(codigo, mensagem, status, **extra):
    return {"erro": {"codigo": codigo, "mensagem": mensagem, **extra}}, status


def _publico(resumo):
    # Nunca expor o caminho no servidor.
    return {"data": resumo["data"], "registros": resumo["registros"],
            "tamanho_bytes": resumo["tamanho_bytes"], "gerado_em": resumo["gerado_em"],
            "url": f"/downloads/brutos?data={resumo['data']}"}


@downloads_bp.get("/downloads/brutos/datas")
@downloads_bp.get("/api/downloads/brutos/datas")
def listar_datas():
    datas = [_publico(item) for item in datas_disponiveis(diretorio_exportacoes())]
    return {"datas": datas, "total": len(datas), "fonte": FONTE, "licenca": LICENCA,
            "dicionario": "/downloads/brutos/dicionario"}, 200


@downloads_bp.get("/downloads/brutos/dicionario")
@downloads_bp.get("/api/downloads/brutos/dicionario")
def dicionario():
    return dicionario_dados(), 200


@downloads_bp.get("/downloads/brutos")
@downloads_bp.get("/api/downloads/brutos")
def baixar():
    for campo in request.args:
        if campo != "data":
            return _erro("PARAMETRO_INVALIDO", "Parâmetro não reconhecido.", 400, campo=campo)
    valores = request.args.getlist("data")
    if len(valores) != 1:
        return _erro("PARAMETRO_INVALIDO", "Informe data uma única vez, no formato AAAA-MM-DD.",
                     400, campo="data")
    try:
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", valores[0].strip()):
            raise ValueError()
        dia = date.fromisoformat(valores[0].strip())
    except ValueError:
        return _erro("PARAMETRO_INVALIDO", "Informe uma data válida no formato AAAA-MM-DD.",
                     400, campo="data")

    resumo = arquivo_completo(diretorio_exportacoes(), dia)
    if resumo is None:
        try:
            prazo = prazo_publicacao(dia)
        except OverflowError:  # 9999-12-31: D+1 não é representável
            prazo = None
        if prazo is None or agora_utc() < prazo:
            return _erro("ARQUIVO_AINDA_INDISPONIVEL",
                         "Os dados deste dia ainda não foram consolidados.", 404,
                         previsto_ate=prazo.isoformat() if prazo else None)
        return _erro("ARQUIVO_INEXISTENTE", "Não há arquivo de brutos publicado para esta data.", 404)
    resposta = send_file(resumo["caminho"], mimetype=MIME_PARQUET, as_attachment=True,
                         download_name=nome_arquivo(dia), max_age=0)
    resposta.headers["X-Content-Type-Options"] = "nosniff"
    resposta.headers["X-Licenca"] = LICENCA
    return resposta


@downloads_bp.get("/downloads/indicadores/metadados")
@downloads_bp.get("/api/downloads/indicadores/metadados")
def metadados_indicadores_csv():
    return disponibilidade_csv(), 200


@downloads_bp.get("/downloads/indicadores")
@downloads_bp.get("/api/downloads/indicadores")
def baixar_indicadores_csv():
    permitidos = {"formato", "data", "linha", "sentido", "tipo", "indicador",
                  "parada", "trecho", "versao_gtfs_id", "inicio", "fim"}
    for campo in request.args:
        if campo not in permitidos:
            return _erro("PARAMETRO_INVALIDO", "Parâmetro não reconhecido.", 400, campo=campo)
        if len(request.args.getlist(campo)) != 1:
            return _erro("PARAMETRO_INVALIDO", "Informe cada parâmetro uma única vez.", 400,
                         campo=campo)
    if request.args.get("formato", "").strip().lower() != "csv" or len(request.args.getlist("formato")) != 1:
        return _erro("FORMATO_NAO_SUPORTADO", "Informe formato=csv.", 400, campo="formato")
    if "data" in request.args and ({"inicio", "fim"} & set(request.args)):
        return _erro("PARAMETRO_INVALIDO", "Use data ou início/fim, não ambos.", 400, campo="data")
    if ("data" not in request.args and
            not {"inicio", "fim"}.issubset(request.args)):
        return _erro("PARAMETRO_INVALIDO", "Informe data ou inicio e fim para selecionar o período.",
                     400, campo="data")
    try:
        filtros = ler_filtros(MultiDict(
            (chave, valor) for chave, valor in request.args.items(multi=True)
            if chave not in ("data", "formato")
        ))
        dia = None
        if "data" in request.args:
            texto = request.args["data"].strip()
            if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", texto):
                raise ValueError("data deve usar AAAA-MM-DD")
            dia = date.fromisoformat(texto)
            filtros["inicio"], filtros["fim"] = periodo_utc(dia)
        else:
            inicio, fim = filtros["inicio"], filtros["fim"]
            if inicio is None or fim is None:
                return _erro("PARAMETRO_INVALIDO", "Informe inicio e fim juntos.", 400, campo="inicio")
            dia = inicio.astimezone(FUSO).date()
            limite_inicio, limite_fim = periodo_utc(dia)
            if inicio < limite_inicio or fim > limite_fim:
                return _erro("PARAMETRO_INVALIDO", "O período deve estar contido em um único dia de São Paulo.",
                             400, campo="inicio")
    except ParametroInvalido as erro:
        return _erro("PARAMETRO_INVALIDO", str(erro), 400, campo=erro.campo)
    except (ValueError, OverflowError):
        return _erro("PARAMETRO_INVALIDO", "Informe data válida no formato AAAA-MM-DD.",
                     400, campo="data")

    try:
        arquivo = arquivo_indicadores_completo(dia)
    except SQLAlchemyError as erro:
        db.session.rollback()
        current_app.logger.error("Falha ao verificar CSV de indicadores (%s)", type(erro).__name__)
        return _erro("DOWNLOAD_INDISPONIVEL", "Não foi possível verificar o arquivo consolidado.", 503)
    if arquivo is None:
        try:
            prazo = prazo_publicacao(dia)
        except OverflowError:
            prazo = None
        if prazo is not None and agora_utc() < prazo:
            return _erro("PERIODO_AINDA_NAO_CONSOLIDADO",
                         "O CSV deste período ainda não foi consolidado e publicado.", 404,
                         data=dia.isoformat(), previsto_em=prazo.astimezone(FUSO).isoformat())
        return _erro("CONSOLIDACAO_NAO_PUBLICADA",
                     "A consolidação deste período ainda não está disponível para download.", 404,
                     data=dia.isoformat())

    try:
        leitor = csv.DictReader(io.StringIO(arquivo["conteudo"].decode("utf-8-sig")))
        buffer = io.StringIO(newline="")
        escritor = csv.DictWriter(buffer, fieldnames=CAMPOS_INDICADORES, lineterminator="\r\n")
        escritor.writeheader()
        total = 0
        for registro in leitor:
            if filtros["linha"] is not None and registro["linha"] != filtros["linha"]:
                continue
            if filtros["sentido"] is not None and registro["sentido"] != filtros["sentido"]:
                continue
            if filtros["tipo"] is not None and registro["tipo"] != filtros["tipo"]:
                continue
            if filtros["parada"] is not None and registro["parada"] != filtros["parada"]:
                continue
            if filtros["trecho"] is not None and registro["trecho"] != filtros["trecho"]:
                continue
            if (filtros["versao_gtfs_id"] is not None
                    and registro["versao_gtfs_id"].lower() != filtros["versao_gtfs_id"]):
                continue
            inicio_registro = datetime.fromisoformat(registro["inicio"].replace("Z", "+00:00"))
            fim_registro = datetime.fromisoformat(registro["fim"].replace("Z", "+00:00"))
            inicio_filtro, fim_filtro = filtros["inicio"], filtros["fim"]
            if inicio_filtro is not None and fim_registro <= inicio_filtro:
                continue
            if fim_filtro is not None:
                cruza_ponto = inicio_filtro is not None and fim_filtro == inicio_filtro
                if (cruza_ponto and inicio_registro > fim_filtro) or (
                    not cruza_ponto and inicio_registro >= fim_filtro
                ):
                    continue
            escritor.writerow(registro)
            total += 1
        conteudo = buffer.getvalue().encode("utf-8-sig")
    except (UnicodeDecodeError, csv.Error, ValueError, KeyError) as erro:
        current_app.logger.error("CSV diário de indicadores inválido (%s)", type(erro).__name__)
        return _erro("ARQUIVO_INVALIDO", "O arquivo consolidado não pôde ser lido.", 503)

    resposta = current_app.response_class(
        conteudo, content_type="text/csv; charset=utf-8",
    )
    resposta.headers["Content-Disposition"] = f'attachment; filename="indicadores_{dia.isoformat()}.csv"'
    resposta.headers["X-Fonte"] = FONTE_INDICADORES
    resposta.headers["X-Fonte-URL"] = FONTE_INDICADORES_URL
    resposta.headers["X-Licenca"] = LICENCA_INDICADORES
    resposta.headers["X-Licenca-URL"] = LICENCA_INDICADORES_URL
    resposta.headers["X-Disponibilizado-Em"] = arquivo["recibo"]["gerado_em"]
    resposta.headers["X-Total-Registros"] = str(total)
    resposta.headers["Link"] = '</downloads/indicadores/metadados>; rel="describedby"; type="application/json"'
    resposta.headers["X-Content-Type-Options"] = "nosniff"
    return resposta
