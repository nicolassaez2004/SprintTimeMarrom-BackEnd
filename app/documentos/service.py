"""Catalogação de PDFs oficiais com metadados (US05-T01).

O PDF é publicado sem alteração no FileStore (chave pelo SHA-256) e seus
metadados vão para `arquivos_armazenados` + `documentos_oficiais`, na mesma
transação. Os metadados são informados por quem cataloga; nada é inferido.
"""

import os
import shutil
import tempfile
from datetime import date
from pathlib import Path

from flask import current_app
from sqlalchemy.orm import joinedload

from app import db
from app.models import PRAZO_CATALOGACAO, ArquivoArmazenado, DocumentoOficial, agora_utc
from app.storage import FileStore, hash_arquivo


def _armazenamento(diretorio=None):
    return FileStore(diretorio or current_app.config.get("DOCUMENTOS_STORAGE_DIR")
                     or Path(current_app.instance_path) / "documentos")


def _texto(nome, valor):
    valor = (valor or "").strip()
    if not valor:
        raise ValueError(f"{nome} é obrigatório")
    return valor


def _eh_pdf(caminho):
    # A especificação admite o cabeçalho %PDF- dentro dos primeiros 1024 bytes.
    with open(caminho, "rb") as arquivo:
        return b"%PDF-" in arquivo.read(1024)


def catalogar_documento(caminho_pdf, *, titulo, tipo, data_documento, orgao, fonte,
                        descoberto_em, diretorio=None, agora=None):
    """Guarda o PDF original e cataloga o documento. Retorna (documento, criado).

    Repetir a chamada com o mesmo PDF e os mesmos metadados devolve o registro
    existente, sem duplicar arquivo nem documento.
    """
    titulo, tipo = _texto("título", titulo), _texto("tipo", tipo)
    orgao, fonte = _texto("órgão", orgao), _texto("fonte", fonte)
    if not isinstance(data_documento, date):
        raise ValueError("data do documento deve ser uma data")
    if descoberto_em is None or descoberto_em.tzinfo is None:
        raise ValueError("data de descoberta deve conter fuso horário")
    agora = agora or agora_utc()
    if descoberto_em > agora:
        raise ValueError("data de descoberta não pode ser posterior à catalogação")
    caminho_pdf = Path(caminho_pdf)
    if not caminho_pdf.is_file() or not _eh_pdf(caminho_pdf):
        raise ValueError(f"arquivo PDF inválido: {caminho_pdf.name}")

    armazenamento = _armazenamento(diretorio)
    # Cópia no mesmo volume do destino: a publicação por link é atômica.
    fd, temporario = tempfile.mkstemp(prefix="pdf-", suffix=".part", dir=armazenamento.diretorio)
    temporario = Path(temporario)
    try:
        with os.fdopen(fd, "wb") as destino, caminho_pdf.open("rb") as origem:
            shutil.copyfileobj(origem, destino)
            destino.flush()
            os.fsync(destino.fileno())
        digest, tamanho = hash_arquivo(temporario)
        chave = armazenamento.publicar_pdf(temporario, digest, tamanho)

        arquivo = ArquivoArmazenado.query.filter_by(chave=chave).one_or_none()
        if arquivo is None:
            arquivo = ArquivoArmazenado(
                chave=chave, nome_original=caminho_pdf.name, mime_type="application/pdf",
                tamanho_bytes=tamanho, sha256=digest, criado_em=agora,
            )
            db.session.add(arquivo)
        else:
            existente = DocumentoOficial.query.filter_by(
                arquivo_id=arquivo.id, titulo=titulo, tipo=tipo,
                data_documento=data_documento, orgao=orgao,
            ).first()
            if existente is not None:
                return existente, False

        documento = DocumentoOficial(
            titulo=titulo, tipo=tipo, data_documento=data_documento, orgao=orgao,
            fonte=fonte, descoberto_em=descoberto_em, catalogado_em=agora, arquivo=arquivo,
        )
        db.session.add(documento)
        db.session.commit()
        return documento, True
    except Exception:
        db.session.rollback()
        # O objeto final é preservado: pode estar referenciado por outro registro.
        raise
    finally:
        temporario.unlink(missing_ok=True)


def _obter(documento_id):
    documento = db.session.get(DocumentoOficial, documento_id) if documento_id else None
    if documento is None:
        raise LookupError(f"documento oficial inexistente: {documento_id!r}")
    return documento


def consultar_documento(documento_id):
    """Metadados, procedência do PDF e verificação do prazo de catalogação."""
    return _serializar(_obter(documento_id))


def buscar_documentos(*, tipo=None, data_inicio=None, data_fim=None, pagina=1, por_pagina=20):
    """Página do acervo; filtros combináveis por tipo e intervalo de data_documento."""
    consulta = DocumentoOficial.query.options(joinedload(DocumentoOficial.arquivo))
    if tipo is not None:
        consulta = consulta.filter(DocumentoOficial.tipo == tipo)
    if data_inicio is not None:
        consulta = consulta.filter(DocumentoOficial.data_documento >= data_inicio)
    if data_fim is not None:
        consulta = consulta.filter(DocumentoOficial.data_documento <= data_fim)
    total = consulta.order_by(None).count()
    itens = (consulta.order_by(DocumentoOficial.data_documento.desc(), DocumentoOficial.id)
             .offset((pagina - 1) * por_pagina).limit(por_pagina).all())
    return {
        "itens": [_serializar(documento) for documento in itens],
        "paginacao": {"pagina": pagina, "por_pagina": por_pagina, "total": total,
                      "paginas": -(-total // por_pagina)},
    }


def _serializar(documento):
    arquivo = documento.arquivo
    return {
        "id": documento.id,
        "titulo": documento.titulo,
        "tipo": documento.tipo,
        "data_documento": documento.data_documento.isoformat(),
        "orgao": documento.orgao,
        "fonte": documento.fonte,
        "descoberto_em": documento.descoberto_em.isoformat(),
        "catalogado_em": documento.catalogado_em.isoformat(),
        "prazo": {
            "limite_dias": PRAZO_CATALOGACAO.days,
            "horas_ate_catalogacao": round(documento.tempo_ate_catalogacao.total_seconds() / 3600, 2),
            "cumprido": documento.catalogado_no_prazo,
        },
        "arquivo": {
            "id": arquivo.id,
            "chave": arquivo.chave,
            "nome_original": arquivo.nome_original,
            "mime_type": arquivo.mime_type,
            "tamanho_bytes": arquivo.tamanho_bytes,
            "sha256": arquivo.sha256,
        },
    }


def caminho_original(documento_id, diretorio=None):
    """Caminho do PDF original, conferido pelo SHA-256 registrado."""
    arquivo = _obter(documento_id).arquivo
    if arquivo.mime_type != "application/pdf":
        # Só entrega PDFs catalogados; ArquivoArmazenado também guarda ZIP/JSON.
        raise FileNotFoundError("arquivo do documento não é um PDF catalogado")
    return _armazenamento(diretorio).caminho_pdf(arquivo.chave, arquivo.sha256)
