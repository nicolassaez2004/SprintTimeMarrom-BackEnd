"""US09-T03: compara todos os campos do arquivo com um snapshot dos brutos."""

import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path

import pyarrow.parquet as pq
from sqlalchemy import select

from app import db
from app.exportacoes.parquet import (
    _SCHEMA,
    FONTE,
    FONTE_URL,
    LICENCA,
    LICENCA_URL,
    MARGEM_FECHAMENTO,
    periodo_utc,
    prazo_publicacao,
)
from app.models import RegistroBrutoGPS
from app.storage import hash_arquivo


def _normalizar(registro, *, banco=False):
    resultado = []
    for nome in _SCHEMA.names:
        valor = registro[nome]
        if isinstance(valor, datetime):
            valor = valor.astimezone(timezone.utc).isoformat()
        if nome == "payload" and banco and valor is not None and not isinstance(valor, str):
            # Mesma representação do exportador. Um payload que já é texto
            # deve permanecer literal: interpretar esse texto como JSON pode
            # esconder uma alteração de espaços/conteúdo do valor original.
            valor = json.dumps(valor, ensure_ascii=False, separators=(",", ":"))
        resultado.append(valor)
    return json.dumps(resultado, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode() + b"\n"


def conferir_parquet(dia: date, caminho, *, verificado_em=None):
    """Sem modificar brutos. Data de geração/mtime não provam publicação."""
    caminho = Path(caminho)
    inicio, fim = periodo_utc(dia)
    parquet = pq.ParquetFile(caminho)
    extras = parquet.schema_arrow.metadata or {}
    metadados_ok = parquet.schema_arrow.equals(_SCHEMA) and all(
        extras.get(chave.encode()) == valor.encode() for chave, valor in {
            "source": FONTE, "source_url": FONTE_URL, "license": LICENCA,
            "license_url": LICENCA_URL, "date": dia.isoformat(),
            "period_start_utc": inicio.isoformat(), "period_end_utc": fim.isoformat(),
        }.items()
    )
    try:
        gerado_em = datetime.fromisoformat(extras[b"generated_at_utc"].decode())
        metadados_ok = metadados_ok and gerado_em.tzinfo is not None and gerado_em >= fim + MARGEM_FECHAMENTO
    except (KeyError, ValueError, TypeError):
        metadados_ok = False
        gerado_em = None
    hashes = [hashlib.sha256(), hashlib.sha256()]
    contagens = [0, 0]
    nulos = [{nome: 0 for nome in _SCHEMA.names} for _ in range(2)]

    def adicionar(origem, registro):
        hashes[origem].update(_normalizar(registro, banco=origem == 0))
        contagens[origem] += 1
        for nome in _SCHEMA.names:
            nulos[origem][nome] += registro[nome] is None

    # O exportador ordena por coletado_em/id. A comparação abrange o mesmo
    # intervalo semiaberto, incluindo NULL, payload e precisão de coordenadas.
    consulta = select(*[getattr(RegistroBrutoGPS, nome) for nome in _SCHEMA.names]).where(
        RegistroBrutoGPS.coletado_em >= inicio, RegistroBrutoGPS.coletado_em < fim,
    ).order_by(RegistroBrutoGPS.coletado_em, RegistroBrutoGPS.id)
    with db.engine.connect() as conn:
        if conn.dialect.name == "mysql":
            conn = conn.execution_options(isolation_level="REPEATABLE READ")
        with conn.begin():
            for registro in conn.execution_options(stream_results=True).execute(consulta).mappings():
                adicionar(0, registro)
    for lote in parquet.iter_batches(batch_size=5000):
        for registro in lote.to_pylist():
            adicionar(1, registro)
    digest, tamanho = hash_arquivo(caminho)
    conteudo_ok = contagens[0] == contagens[1] and hashes[0].digest() == hashes[1].digest()
    recibo = None
    publicado_em = None
    try:
        recibo = json.loads(caminho.with_suffix(".publicacao.json").read_text(encoding="utf-8"))
        publicado_em = datetime.fromisoformat(recibo["publicado_em"])
        if (publicado_em.tzinfo is None or recibo["sha256"] != digest
                or recibo["data"] != dia.isoformat() or recibo["registros"] != contagens[1]
                or gerado_em is None or gerado_em.tzinfo is None or publicado_em < gerado_em):
            publicado_em = None
    except (OSError, ValueError, KeyError, TypeError):
        publicado_em = None
    verificado_em = verificado_em or datetime.now(timezone.utc)
    prazo_ok = (publicado_em <= prazo_publicacao(dia) and publicado_em <= verificado_em
                and publicado_em >= fim) if publicado_em else None
    return {
        "historia": "US09-T03", "data": dia.isoformat(),
        "verificado_em": verificado_em.isoformat(), "conteudo_confere": conteudo_ok,
        "metadados_conferem": metadados_ok, "fonte": FONTE, "licenca": LICENCA,
        "registros_banco": contagens[0], "registros_parquet": contagens[1],
        "sha256_linhas_banco": hashes[0].hexdigest(),
        "sha256_linhas_parquet": hashes[1].hexdigest(), "sha256_arquivo": digest,
        "tamanho_bytes": tamanho, "nulos_banco": nulos[0], "nulos_parquet": nulos[1],
        "publicado_em": publicado_em.isoformat() if publicado_em else None,
        "prazo": prazo_publicacao(dia).isoformat(), "prazo_atendido": prazo_ok,
        "periodicidade": "DIARIA", "fuso": "America/Sao_Paulo",
        "aprovado": conteudo_ok and metadados_ok and prazo_ok is True,
        "observacao": "Recibo ligado ao SHA-256 comprova o horário registrado pelo servidor; "
                      "geração e mtime isolados não comprovam publicação. Dados tardios podem exigir republicação.",
    }
