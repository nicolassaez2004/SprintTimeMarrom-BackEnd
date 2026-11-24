"""Exportação CSV dos indicadores publicados pela API (US10)."""

import csv
import hashlib
import io
import json
import os
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import and_, or_

from app import db
from app.indicadores.service import filtrar_periodo, consulta_base, serializar
from app.models import ExecucaoProcessamento, IndicadorOperacional, Linha
from app.operacao.arquivos import gravar_json
from app.exportacoes.parquet import diretorio_exportacoes, periodo_utc

FONTE = "Observa SP; indicadores calculados a partir de dados públicos da SPTrans"
FONTE_URL = "https://www.sptrans.com.br/desenvolvedores/"
LICENCA = (
    "Condições de acesso da SPTrans: acesso gratuito; dados não podem ser alugados, "
    "sublicenciados ou vendidos. Licença própria do indicador derivado não declarada."
)
LICENCA_URL = (
    "https://www.sptrans.com.br/desenvolvedores/cadastro-desenvolvedores/"
    "registro-desenvolvedores-pj"
)

CAMPOS = (
    "linha", "nome_linha", "sentido", "tipo", "valor", "unidade", "inicio", "fim",
    "parada", "trecho", "versao_gtfs_id", "versao_conjunto",
    "versao_correspondencia", "versao_calculo", "contagem_numerador",
    "contagem_denominador", "qualidade",
)


def _serializar_linhas(registros):
    pares = {(r.versao_gtfs_id, r.linha_codigo) for r in registros}
    nomes = {}
    if pares:
        condicoes = [and_(Linha.versao_gtfs_id == versao, Linha.codigo == codigo)
                     for versao, codigo in pares]
        nomes = {(versao, codigo): nome for versao, codigo, nome in db.session.query(
            Linha.versao_gtfs_id, Linha.codigo, Linha.nome).filter(or_(*condicoes)).all()}
    buffer = io.StringIO(newline="")
    escritor = csv.DictWriter(
        buffer, fieldnames=CAMPOS, extrasaction="ignore", lineterminator="\r\n",
    )
    escritor.writeheader()
    for modelo in registros:
        registro = serializar(modelo)
        linha = {
            **registro,
            "nome_linha": nomes.get((registro["versao_gtfs_id"], registro["linha"]), ""),
            "contagem_numerador": registro["contagens"]["numerador"],
            "contagem_denominador": registro["contagens"]["denominador"],
            "qualidade": json.dumps(registro["qualidade"], ensure_ascii=False,
                                     separators=(",", ":")),
        }
        escritor.writerow(linha)
    return buffer.getvalue().encode("utf-8-sig")


def nome_arquivo_indicadores(dia):
    return f"indicadores_{dia.isoformat()}.csv"


def caminho_indicadores(dia):
    return diretorio_exportacoes() / "indicadores" / nome_arquivo_indicadores(dia)


def publicar_csv_indicadores(dia, agora):
    """Publica atomicamente o CSV completo após a consolidação US23-T03."""
    if isinstance(dia, datetime) or not isinstance(dia, date):
        raise TypeError("dia deve ser datetime.date")
    if agora.tzinfo is None:
        raise ValueError("agora deve conter fuso horário")
    chave = f"US23-T03:CONSOLIDACAO_DIA:{dia.isoformat()}"
    execucao = ExecucaoProcessamento.query.filter_by(chave_logica=chave, estado="PRONTO").one_or_none()
    if execucao is None:
        raise ValueError("o dia precisa estar consolidado antes da publicação")
    existente = arquivo_indicadores_completo(dia)
    if existente is not None and existente["recibo"]["execucao_id"] == execucao.id:
        return existente["recibo"]

    inicio, fim = periodo_utc(dia)
    filtros = {"linha": None, "sentido": None, "tipo": None, "parada": None, "trecho": None,
               "versao_gtfs_id": None, "inicio": inicio, "fim": fim}
    consulta = filtrar_periodo(consulta_base(filtros), filtros)
    registros = consulta.order_by(
        IndicadorOperacional.periodo_inicio.asc(), IndicadorOperacional.linha_codigo.asc(),
        IndicadorOperacional.sentido.asc(), IndicadorOperacional.indicador.asc(),
        IndicadorOperacional.id.asc(),
    ).all()
    conteudo = _serializar_linhas(registros)
    destino = caminho_indicadores(dia)
    destino.parent.mkdir(parents=True, exist_ok=True)
    temporario = None
    try:
        with tempfile.NamedTemporaryFile(prefix=f".{destino.name}.", suffix=".tmp",
                                         dir=destino.parent, delete=False) as arquivo:
            temporario = Path(arquivo.name)
            arquivo.write(conteudo)
            arquivo.flush()
            os.fsync(arquivo.fileno())
        os.chmod(temporario, 0o644)
        os.replace(temporario, destino)
        temporario = None
        hash_csv = hashlib.sha256(conteudo).hexdigest()
        recibo = {
            "data": dia.isoformat(), "gerado_em": agora.astimezone(timezone.utc).isoformat(),
            "execucao_id": execucao.id, "execucao_chave": chave,
            "sha256": hash_csv, "tamanho_bytes": len(conteudo), "registros": len(registros),
            "fonte": FONTE, "licenca": LICENCA,
        }
        gravar_json(Path(f"{destino}.publicacao.json"), recibo)
        return recibo
    finally:
        if temporario is not None:
            temporario.unlink(missing_ok=True)


def arquivo_indicadores_completo(dia):
    """Confere recibo, hash e consolidação antes de expor o arquivo ao público."""
    destino = caminho_indicadores(dia)
    recibo_path = Path(f"{destino}.publicacao.json")
    try:
        recibo = json.loads(recibo_path.read_text(encoding="utf-8"))
        conteudo = destino.read_bytes()
        execucao = ExecucaoProcessamento.query.filter_by(
            chave_logica=recibo["execucao_chave"], estado="PRONTO").one_or_none()
        if (recibo["data"] != dia.isoformat() or execucao is None
                or recibo["execucao_id"] != execucao.id
                or recibo["sha256"] != hashlib.sha256(conteudo).hexdigest()
                or recibo["tamanho_bytes"] != len(conteudo)):
            return None
        return {"caminho": destino, "recibo": recibo, "conteudo": conteudo}
    except (OSError, KeyError, ValueError, TypeError):
        return None


def disponibilidade_csv():
    """Metadados que acompanham o arquivo baixado e seus cabeçalhos HTTP."""
    return {
        "fonte": FONTE,
        "fonte_url": FONTE_URL,
        "licenca": LICENCA,
        "licenca_url": LICENCA_URL,
        "licenca_propria_indicadores": "não declarada",
        "disponibilidade": "CSV diário publicado após consolidação; disponível até 06:00 do dia seguinte",
        "codificacao": "UTF-8 com BOM",
        "separador": ",",
        "nulos": "campos vazios",
        "fuso": "instantes ISO 8601 com offset preservado da API",
        "fuso_publicacao": "America/Sao_Paulo",
        "url": "/downloads/indicadores?formato=csv&data=AAAA-MM-DD",
    }
