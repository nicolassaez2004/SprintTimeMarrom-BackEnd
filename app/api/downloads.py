"""Downloads públicos dos brutos diários em Parquet (US09-T02). Somente leitura."""

import csv
import io
import re
from datetime import date, timedelta

from flask import Blueprint, current_app, request, send_file
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.datastructures import MultiDict

from app import db
from app.api.comum import ParametroInvalido, erro_json
from app.api.indicadores import _CODIGO, ler_filtros
from app.indicadores.agenda import COMPOSTOS
from app.exportacoes.csv import (
    FONTE as FONTE_INDICADORES,
    FONTE_URL as FONTE_INDICADORES_URL,
    LICENCA as LICENCA_INDICADORES,
    LICENCA_URL as LICENCA_INDICADORES_URL,
    CAMPOS as CAMPOS_INDICADORES,
    VersaoIndisponivel,
    arquivo_indicadores_completo,
    disponibilidade_csv,
    filtrar_linhas,
    linhas_da_versao,
)
from app.exportacoes.parquet import (
    FONTE, FUSO, LICENCA, MIME_PARQUET, arquivo_completo, datas_disponiveis,
    diretorio_exportacoes, dicionario_dados, nome_arquivo, prazo_publicacao, periodo_utc,
)
from app.models import agora_utc

downloads_bp = Blueprint("downloads", __name__)


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
            return erro_json("PARAMETRO_INVALIDO", "Parâmetro não reconhecido.", 400, campo=campo)
    valores = request.args.getlist("data")
    if len(valores) != 1:
        return erro_json("PARAMETRO_INVALIDO", "Informe data uma única vez, no formato AAAA-MM-DD.",
                     400, campo="data")
    try:
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", valores[0].strip()):
            raise ValueError()
        dia = date.fromisoformat(valores[0].strip())
    except ValueError:
        return erro_json("PARAMETRO_INVALIDO", "Informe uma data válida no formato AAAA-MM-DD.",
                     400, campo="data")

    resumo = arquivo_completo(diretorio_exportacoes(), dia)
    if resumo is None:
        try:
            prazo = prazo_publicacao(dia)
        except OverflowError:  # 9999-12-31: D+1 não é representável
            prazo = None
        if prazo is None or agora_utc() < prazo:
            return erro_json("ARQUIVO_AINDA_INDISPONIVEL",
                         "Os dados deste dia ainda não foram consolidados.", 404,
                         previsto_ate=prazo.isoformat() if prazo else None)
        return erro_json("ARQUIVO_INEXISTENTE", "Não há arquivo de brutos publicado para esta data.", 404)
    resposta = send_file(resumo["caminho"], mimetype=MIME_PARQUET, as_attachment=True,
                         download_name=nome_arquivo(dia), max_age=0)
    resposta.headers["X-Content-Type-Options"] = "nosniff"
    resposta.headers["X-Licenca"] = LICENCA
    return resposta


@downloads_bp.get("/downloads/indicadores/metadados")
@downloads_bp.get("/api/downloads/indicadores/metadados")
def metadados_indicadores_csv():
    return disponibilidade_csv(), 200


_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
PERIODO_VERSAO_MAX = timedelta(days=31)


@downloads_bp.get("/downloads/indicadores")
@downloads_bp.get("/api/downloads/indicadores")
def baixar_indicadores_csv():
    # US25-T01: com versao_dados, exporta a versão exibida em /series (e aceita a faixa diária).
    versao = request.args.get("versao_dados", "").strip().lower() or None
    permitidos = {"formato", "data", "linha", "sentido", "tipo", "indicador",
                  "parada", "trecho", "versao_gtfs_id", "inicio", "fim", "versao_dados"}
    if versao is not None:
        permitidos |= {"hora_inicio", "hora_fim"}
    for campo in request.args:
        if campo not in permitidos:
            return erro_json("PARAMETRO_INVALIDO", "Parâmetro não reconhecido.", 400, campo=campo)
        if len(request.args.getlist(campo)) != 1:
            return erro_json("PARAMETRO_INVALIDO", "Informe cada parâmetro uma única vez.", 400,
                         campo=campo)
    if "versao_dados" in request.args and (versao is None or not _UUID.fullmatch(versao)):
        return erro_json("PARAMETRO_INVALIDO", "Informe versao_dados como recebido em GET /series.",
                         400, campo="versao_dados")
    if request.args.get("formato", "").strip().lower() != "csv" or len(request.args.getlist("formato")) != 1:
        return erro_json("FORMATO_NAO_SUPORTADO", "Informe formato=csv.", 400, campo="formato")
    if "data" in request.args and ({"inicio", "fim"} & set(request.args)):
        return erro_json("PARAMETRO_INVALIDO", "Use data ou início/fim, não ambos.", 400, campo="data")
    if ("data" not in request.args and
            not {"inicio", "fim"}.issubset(request.args)):
        return erro_json("PARAMETRO_INVALIDO", "Informe data ou inicio e fim para selecionar o período.",
                     400, campo="data")
    if "tipo" in request.args and "indicador" in request.args:
        return erro_json("PARAMETRO_INVALIDO", "Use tipo ou indicador, não ambos.", 400, campo="tipo")
    tipo = (request.args.get("tipo") or request.args.get("indicador") or "").strip() or None
    if tipo is not None and not _CODIGO.fullmatch(tipo):
        return erro_json("PARAMETRO_INVALIDO", "Use de 1 a 64 letras, números ou os caracteres _ . : -.",
                         400, campo="tipo")
    # Mesmos tipos públicos de GET /indicadores: ESPERA e FREQUENCIA reúnem dois indicadores.
    # O tipo é filtrado aqui: as regras de agregação da consulta não se aplicam ao arquivo diário.
    tipos = set(COMPOSTOS.get(tipo, (tipo,))) if tipo else None
    try:
        filtros = ler_filtros(MultiDict(
            (chave, valor) for chave, valor in request.args.items(multi=True)
            if chave not in ("data", "formato", "tipo", "indicador", "versao_dados")
        ), serie=versao is not None)
        dia = None
        if "data" in request.args:
            texto = request.args["data"].strip()
            if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", texto):
                raise ValueError("data deve usar AAAA-MM-DD")
            dia = date.fromisoformat(texto)
            filtros["inicio"], filtros["fim"] = periodo_utc(dia)
        elif versao is not None:
            # A versão é recomposta de arquivos imutáveis: o período pode cruzar dias.
            if filtros["fim"] - filtros["inicio"] > PERIODO_VERSAO_MAX:
                return erro_json("PARAMETRO_INVALIDO", "Com versao_dados, o período tem até 31 dias.",
                                 400, campo="inicio")
        else:
            inicio, fim = filtros["inicio"], filtros["fim"]
            if inicio is None or fim is None:
                return erro_json("PARAMETRO_INVALIDO", "Informe inicio e fim juntos.", 400, campo="inicio")
            dia = inicio.astimezone(FUSO).date()
            limite_inicio, limite_fim = periodo_utc(dia)
            if inicio < limite_inicio or fim > limite_fim:
                return erro_json("PARAMETRO_INVALIDO", "O período deve estar contido em um único dia de São Paulo.",
                             400, campo="inicio")
    except ParametroInvalido as erro:
        return erro_json("PARAMETRO_INVALIDO", str(erro), 400, campo=erro.campo)
    except (ValueError, OverflowError):
        return erro_json("PARAMETRO_INVALIDO", "Informe data válida no formato AAAA-MM-DD.",
                     400, campo="data")

    if versao is not None:
        try:
            publicacao, linhas = linhas_da_versao(versao, filtros, tipos)
        except VersaoIndisponivel as erro:
            return erro_json(erro.codigo, str(erro), erro.status, **erro.extra)
        except SQLAlchemyError as erro:
            db.session.rollback()
            current_app.logger.error("Falha ao recompor versão de indicadores (%s)", type(erro).__name__)
            return erro_json("DOWNLOAD_INDISPONIVEL", "Não foi possível recompor a versão solicitada.", 503)
        linhas = filtrar_linhas(linhas, filtros, tipos, serie=True)
        local = [i.astimezone(FUSO).date().isoformat() for i in (filtros["inicio"], filtros["fim"])]
        nome = f"indicadores_{local[0]}_{local[1]}_v{publicacao.id[:8]}.csv"
        cabecalhos = {"X-Versao-Dados": publicacao.id,
                      "X-Versao-Publicada-Em": publicacao.finalizada_em.isoformat(),
                      "X-Disponibilizado-Em": publicacao.finalizada_em.isoformat()}
    else:
        try:
            arquivo = arquivo_indicadores_completo(dia)
        except SQLAlchemyError as erro:
            db.session.rollback()
            current_app.logger.error("Falha ao verificar CSV de indicadores (%s)", type(erro).__name__)
            return erro_json("DOWNLOAD_INDISPONIVEL", "Não foi possível verificar o arquivo consolidado.", 503)
        if arquivo is None:
            try:
                prazo = prazo_publicacao(dia)
            except OverflowError:
                prazo = None
            if prazo is not None and agora_utc() < prazo:
                return erro_json("PERIODO_AINDA_NAO_CONSOLIDADO",
                             "O CSV deste período ainda não foi consolidado e publicado.", 404,
                             data=dia.isoformat(), previsto_em=prazo.astimezone(FUSO).isoformat())
            return erro_json("CONSOLIDACAO_NAO_PUBLICADA",
                         "A consolidação deste período ainda não está disponível para download.", 404,
                         data=dia.isoformat())
        try:
            linhas = filtrar_linhas(
                csv.DictReader(io.StringIO(arquivo["conteudo"].decode("utf-8-sig"))), filtros, tipos)
        except UnicodeDecodeError:
            linhas = None
        nome = f"indicadores_{dia.isoformat()}.csv"
        cabecalhos = {"X-Disponibilizado-Em": arquivo["recibo"]["gerado_em"]}

    try:
        if linhas is None:
            raise ValueError("codificação inválida")
        buffer = io.StringIO(newline="")
        escritor = csv.DictWriter(buffer, fieldnames=CAMPOS_INDICADORES, lineterminator="\r\n")
        escritor.writeheader()
        total = 0
        for registro in linhas:
            escritor.writerow(registro)
            total += 1
        conteudo = buffer.getvalue().encode("utf-8-sig")
    except (UnicodeDecodeError, csv.Error, ValueError, KeyError) as erro:
        current_app.logger.error("CSV de indicadores inválido (%s)", type(erro).__name__)
        return erro_json("ARQUIVO_INVALIDO", "O arquivo consolidado não pôde ser lido.", 503)

    resposta = current_app.response_class(
        conteudo, content_type="text/csv; charset=utf-8",
    )
    resposta.headers["Content-Disposition"] = f'attachment; filename="{nome}"'
    resposta.headers["X-Fonte"] = FONTE_INDICADORES
    resposta.headers["X-Fonte-URL"] = FONTE_INDICADORES_URL
    resposta.headers["X-Licenca"] = LICENCA_INDICADORES
    resposta.headers["X-Licenca-URL"] = LICENCA_INDICADORES_URL
    resposta.headers.update(cabecalhos)
    resposta.headers["X-Total-Registros"] = str(total)
    resposta.headers["Link"] = '</downloads/indicadores/metadados>; rel="describedby"; type="application/json"'
    resposta.headers["X-Content-Type-Options"] = "nosniff"
    return resposta
