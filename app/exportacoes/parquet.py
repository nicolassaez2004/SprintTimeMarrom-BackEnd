"""Exportação diária e atômica dos registros GPS brutos para Parquet."""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import and_, or_, select

from flask import current_app

from app import db
from app.core.tempo import FUSO
from app.models import ExecucaoColeta, RegistroBrutoGPS
from app.operacao.arquivos import gravar_json
from app.storage import hash_arquivo


FONTE = "SPTrans Olho Vivo (registros brutos coletados pela aplicação)"
FONTE_URL = "https://api.olhovivo.sptrans.com.br/v2.1/Posicao"
LICENCA = "CC BY 4.0"
LICENCA_URL = "https://creativecommons.org/licenses/by/4.0/"
# Dia operacional de São Paulo, como nas demais rotinas diárias do projeto.
# Folga após a meia-noite para os últimos ciclos do dia serem gravados (US29:
# registro em até 60 s). Exportação anterior a isso não é considerada completa.
MARGEM_FECHAMENTO = timedelta(minutes=10)
# US09: arquivo do dia D disponível até 06:00 de D+1 (America/Sao_Paulo).
HORARIO_LIMITE = time(6)
MIME_PARQUET = "application/vnd.apache.parquet"
# Dias anteriores revistos a cada execução para dados gravados com atraso.
REVISAO_DIAS = 7
_NOME = re.compile(r"registros_gps_([0-9]{4}-[0-9]{2}-[0-9]{2})\.parquet")
logger = logging.getLogger(__name__)

_SCHEMA = pa.schema([
    pa.field("id", pa.int64()),
    pa.field("execucao_id", pa.int64()),
    pa.field("veiculo_prefixo", pa.string()),
    pa.field("prefixo_origem", pa.string()),
    pa.field("linha_origem", pa.string()),
    pa.field("sentido_origem", pa.string()),
    pa.field("latitude", pa.float64()),
    pa.field("longitude", pa.float64()),
    pa.field("horario_gps", pa.timestamp("us", tz="UTC")),
    pa.field("coletado_em", pa.timestamp("us", tz="UTC"), nullable=False),
    # JSON é preservado como texto UTF-8 para comportar objetos heterogêneos.
    pa.field("payload", pa.string()),
])


def periodo_utc(dia):
    """[00:00 de D, 00:00 de D+1) em America/Sao_Paulo, convertido para UTC."""
    inicio = datetime.combine(dia, time.min, tzinfo=FUSO).astimezone(timezone.utc)
    fim = datetime.combine(dia + timedelta(days=1), time.min, tzinfo=FUSO).astimezone(timezone.utc)
    return inicio, fim


def _utc(value):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def gerar_parquet_diario(dia: date, destino, *, tamanho_lote: int = 5000, relogio=None) -> dict:
    """Exporta `coletado_em` no dia de São Paulo informado; publica só ao concluir.

    Os instantes continuam em UTC no arquivo. A consulta é paginada por
    (`coletado_em`, `id`) dentro de uma transação consistente. O arquivo
    temporário fica no diretório de destino; `os.replace` publica atomicamente.
    """
    if isinstance(dia, datetime) or not isinstance(dia, date):
        raise TypeError("dia deve ser datetime.date")
    if tamanho_lote < 1:
        raise ValueError("tamanho_lote deve ser positivo")

    destino = Path(destino)
    destino.parent.mkdir(parents=True, exist_ok=True)
    inicio, fim = periodo_utc(dia)
    # Registrado antes do snapshot da transação: tudo gravado até aqui está no arquivo.
    gerado_em = (relogio or (lambda: datetime.now(timezone.utc)))().astimezone(timezone.utc)
    colunas = [
        RegistroBrutoGPS.id, RegistroBrutoGPS.execucao_id,
        RegistroBrutoGPS.veiculo_prefixo, RegistroBrutoGPS.prefixo_origem,
        RegistroBrutoGPS.linha_origem, RegistroBrutoGPS.sentido_origem,
        RegistroBrutoGPS.latitude, RegistroBrutoGPS.longitude,
        RegistroBrutoGPS.horario_gps, RegistroBrutoGPS.coletado_em,
        RegistroBrutoGPS.payload,
    ]
    metadados = {
        b"source": FONTE.encode(),
        b"source_url": FONTE_URL.encode(),
        b"license": LICENCA.encode(),
        b"license_url": LICENCA_URL.encode(),
        b"date": dia.isoformat().encode(),
        b"date_field": b"coletado_em, dia em America/Sao_Paulo; instantes em UTC",
        b"period_start_utc": inicio.isoformat().encode(),
        b"period_end_utc": fim.isoformat().encode(),
        b"generated_at_utc": gerado_em.isoformat().encode(),
    }
    esquema = _SCHEMA.with_metadata(metadados)
    temp_path = None
    total = 0
    ultimo = None
    writer = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{destino.name}.", suffix=".tmp", dir=destino.parent, delete=False
        ) as temp:
            temp_path = Path(temp.name)
        conexao = db.engine.connect()
        if db.engine.dialect.name == "mysql":
            conexao = conexao.execution_options(isolation_level="REPEATABLE READ")
        with conexao as conn:
            with conn.begin():
                writer = pq.ParquetWriter(temp_path, esquema, compression="snappy")
                while True:
                    consulta = (
                        select(*colunas)
                        .where(RegistroBrutoGPS.coletado_em >= inicio)
                        .where(RegistroBrutoGPS.coletado_em < fim)
                        .order_by(RegistroBrutoGPS.coletado_em, RegistroBrutoGPS.id)
                        .limit(tamanho_lote)
                    )
                    if ultimo is not None:
                        # Keyset em ix_registros_gps_coletado_em_id: não relê o histórico.
                        consulta = consulta.where(or_(
                            RegistroBrutoGPS.coletado_em > ultimo[0],
                            and_(RegistroBrutoGPS.coletado_em == ultimo[0],
                                 RegistroBrutoGPS.id > ultimo[1]),
                        ))
                    lote = conn.execute(consulta).mappings().all()
                    if not lote:
                        break
                    linhas = []
                    for registro in lote:
                        payload = registro["payload"]
                        if payload is not None and not isinstance(payload, str):
                            payload = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                        linhas.append({
                            **{chave: registro[chave] for chave in (
                                "id", "execucao_id", "veiculo_prefixo", "prefixo_origem",
                                "linha_origem", "sentido_origem", "latitude", "longitude",
                            )},
                            "horario_gps": _utc(registro["horario_gps"]),
                            "coletado_em": _utc(registro["coletado_em"]),
                            "payload": payload,
                        })
                    tabela = pa.Table.from_pylist(linhas, schema=esquema)
                    writer.write_table(tabela)
                    total += len(lote)
                    ultimo = (lote[-1]["coletado_em"], lote[-1]["id"])
                writer.close()
                writer = None
                # Windows exige descritor de escrita para _commit/fsync.
                with open(temp_path, "r+b") as arquivo:
                    os.fsync(arquivo.fileno())
        # NamedTemporaryFile cria 0600; o arquivo publicado é de leitura pública.
        os.chmod(temp_path, 0o644)
        os.replace(temp_path, destino)
        temp_path = None
        return {"arquivo": str(destino), "registros": total, "data": dia.isoformat()}
    finally:
        if writer is not None:
            writer.close()
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


# --- Publicação e catálogo (US09-T02) ---------------------------------------

def diretorio_exportacoes():
    return Path(current_app.config.get("EXPORTACOES_DIR")
                or Path(current_app.instance_path) / "exportacoes")


def nome_arquivo(dia):
    return f"registros_gps_{dia.isoformat()}.parquet"


def prazo_publicacao(dia):
    """06:00 de D+1 em São Paulo, em UTC."""
    return datetime.combine(dia + timedelta(days=1), HORARIO_LIMITE, tzinfo=FUSO).astimezone(timezone.utc)


def arquivo_completo(diretorio, dia):
    """Resumo público do Parquet de `dia`, ou None se ausente/parcial/inválido.

    Completo = nome final (temporários ficam ocultos), rodapé Parquet legível,
    esquema da exportação, metadado `date` igual ao dia e geração iniciada após
    o fim do dia + MARGEM_FECHAMENTO. Uma exportação manual do dia corrente não
    passa nesta última regra.
    """
    caminho = Path(diretorio) / nome_arquivo(dia)
    try:
        metadados = pq.read_metadata(caminho)
        extras = metadados.metadata or {}
        _, fim = periodo_utc(dia)
        gerado_em = datetime.fromisoformat(extras[b"generated_at_utc"].decode())
        if (extras.get(b"date") != dia.isoformat().encode()
                or not metadados.schema.to_arrow_schema().equals(_SCHEMA)
                or gerado_em < fim + MARGEM_FECHAMENTO):
            return None
    except (OSError, KeyError, ValueError, pa.ArrowException):
        return None
    return {"caminho": caminho, "data": dia.isoformat(), "registros": metadados.num_rows,
            "tamanho_bytes": caminho.stat().st_size, "gerado_em": gerado_em.isoformat()}


def datas_disponiveis(diretorio):
    """Dias com Parquet completo, do mais recente ao mais antigo."""
    diretorio = Path(diretorio)
    if not diretorio.is_dir():
        return []
    itens = []
    for caminho in diretorio.iterdir():
        encontrado = _NOME.fullmatch(caminho.name)
        if not encontrado:
            continue
        try:
            dia = date.fromisoformat(encontrado.group(1))
        except ValueError:
            continue
        resumo = arquivo_completo(diretorio, dia)
        if resumo is not None:
            itens.append(resumo)
    return sorted(itens, key=lambda item: item["data"], reverse=True)


def dia_a_publicar(agora):
    """Último dia de São Paulo já encerrado há pelo menos MARGEM_FECHAMENTO."""
    local = (agora.astimezone(timezone.utc) - MARGEM_FECHAMENTO).astimezone(FUSO)
    return local.date() - timedelta(days=1)


def _ciclos_do_dia(dia):
    """Ciclos que podem ter gravado registros com `coletado_em` no dia.

    `coletado_em` >= `iniciada_em`; a folga de 1 h cobre ciclos com
    retentativas (US29) iniciados antes da meia-noite.
    """
    inicio, fim = periodo_utc(dia)
    E = ExecucaoColeta
    return db.session.query(E.id).filter(
        E.fonte == "OLHO_VIVO", E.iniciada_em >= inicio - timedelta(hours=1), E.iniciada_em < fim)


def _ciclo_em_aberto(dia):
    E = ExecucaoColeta
    return _ciclos_do_dia(dia).filter(
        or_(E.estado == "EM_ANDAMENTO", E.finalizada_em.is_(None))).first() is not None


def _gravado_apos(dia, gerado_em):
    """Ciclo do dia finalizado depois da exportação: o arquivo pode estar sem ele.

    A US29 grava o ciclo guardado no checkpoint local quando o MySQL volta;
    `finalizada_em` é atualizado na mesma transação dos registros e na confirmação.
    """
    return _ciclos_do_dia(dia).filter(ExecucaoColeta.finalizada_em >= gerado_em).first() is not None


def _exportar(diretorio, dia, relogio, motivo):
    resultado = gerar_parquet_diario(dia, diretorio / nome_arquivo(dia), relogio=relogio)
    publicado_em = relogio().astimezone(timezone.utc)
    caminho = diretorio / nome_arquivo(dia)
    digest, tamanho = hash_arquivo(caminho)
    gravar_json(caminho.with_suffix(".publicacao.json"), {
        "data": dia.isoformat(), "publicado_em": publicado_em.isoformat(),
        "sha256": digest, "tamanho_bytes": tamanho,
        "registros": resultado["registros"], "motivo": motivo,
    })
    no_prazo = publicado_em <= prazo_publicacao(dia)
    logger.log(logging.INFO if no_prazo else logging.ERROR, json.dumps({
        "evento": "brutos_parquet_publicado" if no_prazo else "brutos_parquet_prazo_excedido",
        "motivo": motivo, "data": dia.isoformat(), "registros": resultado["registros"],
        "publicado_em": publicado_em.isoformat(), "prazo": prazo_publicacao(dia).isoformat(),
    }))
    return {**resultado, "publicado_em": publicado_em.isoformat(), "prazo_atendido": no_prazo}


def publicar_brutos(agora, *, diretorio=None, relogio=None):
    """Tarefa periódica do processo `app.tarefas`: idempotente e sem estado.

    1. Gera o Parquet do último dia encerrado se ainda não houver arquivo
       completo. Espera enquanto houver ciclo desse dia em andamento, salvo a
       partir das 06:00 de D+1, quando publica para cumprir o prazo.
    2. Regera arquivos dos últimos REVISAO_DIAS dias quando um ciclo do dia foi
       gravado depois da exportação (dado atrasado). A troca é atômica.

    Retorna o resultado da última exportação, ou None quando não há o que fazer.
    """
    relogio = relogio or (lambda: datetime.now(timezone.utc))
    diretorio = Path(diretorio or diretorio_exportacoes())
    dia = dia_a_publicar(agora)
    resultado = None
    if arquivo_completo(diretorio, dia) is None and (
            agora >= prazo_publicacao(dia) or not _ciclo_em_aberto(dia)):
        resultado = _exportar(diretorio, dia, relogio, "NOVO_DIA")
    for atraso in range(REVISAO_DIAS):
        anterior = dia - timedelta(days=atraso)
        resumo = arquivo_completo(diretorio, anterior)
        if resumo and _gravado_apos(anterior, datetime.fromisoformat(resumo["gerado_em"])):
            resultado = _exportar(diretorio, anterior, relogio, "DADO_ATRASADO")
    return resultado


_DESCRICOES = {
    "id": ("Identificador do registro bruto na base (registros_gps.id).", None),
    "execucao_id": ("Ciclo de coleta (ExecucaoColeta) que persistiu o registro pela primeira vez.", None),
    "veiculo_prefixo": ("Prefixo do veículo no cadastro de veículos; igual a prefixo_origem quando informado.", None),
    "prefixo_origem": ("Prefixo do veículo como recebido da API Olho Vivo (campo p).", None),
    "linha_origem": ("Código interno da linha na API Olho Vivo (campo cl); não é o letreiro.", None),
    "sentido_origem": ("Sentido de operação recebido (campo sl): 1 = terminal principal → secundário; "
                       "2 = secundário → principal. Outros valores ficam nulos.", None),
    "latitude": ("Latitude da posição (campo py). Nula se ausente ou fora de [-90, 90].", "graus decimais"),
    "longitude": ("Longitude da posição (campo px). Nula se ausente ou fora de [-180, 180].", "graus decimais"),
    "horario_gps": ("Instante da posição informado pela API (campo ta). Nulo se ausente ou inválido.",
                    "instante UTC, precisão de microssegundos"),
    "coletado_em": ("Início da consulta à API que produziu o registro. Define o dia do arquivo "
                    "(dia civil em America/Sao_Paulo).", "instante UTC, precisão de microssegundos"),
    "payload": ("Objeto do veículo como recebido da API, serializado como texto JSON UTF-8.", "JSON"),
}


def dicionario_dados():
    """Dicionário gerado a partir do esquema real dos arquivos (_SCHEMA)."""
    return {
        "arquivo": "registros_gps_AAAA-MM-DD.parquet",
        "formato": "Apache Parquet, compressão snappy",
        "tipo_conteudo": MIME_PARQUET,
        "recorte": "coletado_em no dia civil de America/Sao_Paulo; instantes gravados em UTC",
        "disponibilidade": "arquivo do dia D publicado até 06:00 de D+1 (America/Sao_Paulo)",
        "fonte": FONTE, "fonte_url": FONTE_URL, "licenca": LICENCA, "licenca_url": LICENCA_URL,
        "metadados_arquivo": ["source", "source_url", "license", "license_url", "date", "date_field",
                              "period_start_utc", "period_end_utc", "generated_at_utc"],
        "campos": [{"nome": campo.name, "tipo": str(campo.type), "anulavel": campo.nullable,
                    "descricao": _DESCRICOES[campo.name][0], "unidade": _DESCRICOES[campo.name][1]}
                   for campo in _SCHEMA],
    }
