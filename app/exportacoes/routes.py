"""Downloads públicos dos brutos diários em Parquet (US09-T02). Somente leitura."""

import re
from datetime import date

from flask import Blueprint, request, send_file

from app.exportacoes.parquet import (
    FONTE, LICENCA, MIME_PARQUET, arquivo_completo, datas_disponiveis,
    diretorio_exportacoes, dicionario_dados, nome_arquivo, prazo_publicacao,
)
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
