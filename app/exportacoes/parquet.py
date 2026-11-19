"""Exportação diária e atômica dos registros GPS brutos para Parquet."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import and_, or_, select

from app import db
from app.models import RegistroBrutoGPS


FONTE = "SPTrans Olho Vivo (registros brutos coletados pela aplicação)"
FONTE_URL = "https://api.olhovivo.sptrans.com.br/v2.1/Posicao"
LICENCA = "CC BY 4.0"
LICENCA_URL = "https://creativecommons.org/licenses/by/4.0/"
# Dia operacional de São Paulo, como nas demais rotinas diárias do projeto.
FUSO = ZoneInfo("America/Sao_Paulo")

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


def _utc(value):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def gerar_parquet_diario(dia: date, destino, *, tamanho_lote: int = 5000) -> dict:
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
    inicio = datetime.combine(dia, time.min, tzinfo=FUSO).astimezone(timezone.utc)
    fim = datetime.combine(dia + timedelta(days=1), time.min, tzinfo=FUSO).astimezone(timezone.utc)
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
                with open(temp_path, "rb") as arquivo:
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
