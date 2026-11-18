"""Captura, valida e versiona o ZIP original do GTFS por SHA-256."""

import csv
import hashlib
import os
import tempfile
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4

from flask import current_app
from sqlalchemy.exc import IntegrityError

from app import db
from app.integrations.gtfs import GTFSClient
from app.models import ArquivoArmazenado, VersaoGTFS


class GTFSValidationError(ValueError):
    """O download terminou, mas não contém um feed GTFS íntegro."""


def _validar_zip(caminho):
    try:
        with zipfile.ZipFile(caminho) as arquivo:
            if arquivo.testzip() is not None:
                raise GTFSValidationError("ZIP GTFS contém entrada corrompida")
            nomes = {Path(item.filename).name.lower() for item in arquivo.infolist()}
            obrigatorios = {"agency.txt", "stops.txt", "routes.txt", "trips.txt", "stop_times.txt"}
            faltantes = obrigatorios - nomes
            if faltantes or not ({"calendar.txt", "calendar_dates.txt"} & nomes):
                raise GTFSValidationError("ZIP não contém os arquivos básicos de um feed GTFS")
            candidatos = [item for item in arquivo.namelist() if Path(item).name.lower() == "feed_info.txt"]
            if candidatos:
                with arquivo.open(candidatos[0]) as stream:
                    linhas = list(csv.DictReader(line.decode("utf-8-sig") for line in stream))
                if not linhas:
                    raise GTFSValidationError("feed_info.txt está vazio")
                info = linhas[0]
                inicio = _ler_data_gtfs(info.get("feed_start_date"))
                fim = _ler_data_gtfs(info.get("feed_end_date"))
                if inicio and fim and fim < inicio:
                    raise GTFSValidationError("vigência GTFS termina antes do início")
                return inicio, fim
    except (zipfile.BadZipFile, OSError, UnicodeError, csv.Error, KeyError, ValueError) as erro:
        if isinstance(erro, GTFSValidationError):
            raise
        raise GTFSValidationError("download não é um ZIP GTFS íntegro") from erro
    return None, None


def _ler_data_gtfs(valor):
    if not valor:
        return None
    return datetime.strptime(valor, "%Y%m%d").date()


def _hash_arquivo(caminho):
    digest = hashlib.sha256()
    tamanho = 0
    with open(caminho, "rb") as arquivo:
        for bloco in iter(lambda: arquivo.read(1024 * 1024), b""):
            digest.update(bloco)
            tamanho += len(bloco)
    return digest.hexdigest(), tamanho


def capturar_gtfs(*, url=None, diretorio=None, cliente=None, agora=None):
    """Baixa e guarda uma captura imutável; hash repetido retorna a versão atual.

    O arquivo é validado antes de entrar no armazenamento permanente. A versão
    e os metadados do arquivo são confirmados em uma única transação SQL.
    """
    url = url or current_app.config.get("GTFS_SOURCE_URL")
    diretorio = Path(diretorio or (Path(current_app.instance_path) / "gtfs"))
    cliente = cliente or GTFSClient()
    agora = agora or datetime.now(timezone.utc)
    if agora.tzinfo is None:
        raise ValueError("data de captura deve conter fuso horário")
    diretorio.mkdir(parents=True, exist_ok=True)

    fd, temporario = tempfile.mkstemp(prefix="gtfs-", suffix=".part", dir=diretorio)
    os.close(fd)
    caminho_temp = Path(temporario)
    caminho_final = None
    arquivo_criado = False
    try:
        with caminho_temp.open("wb") as destino:
            cliente.baixar_para(url, destino)
            destino.flush()
            os.fsync(destino.fileno())

        inicio_feed, fim_feed = _validar_zip(caminho_temp)
        digest, tamanho = _hash_arquivo(caminho_temp)
        existente = VersaoGTFS.query.filter_by(hash=digest).first()
        if existente:
            return existente

        chave = f"gtfs/{digest}.zip"
        caminho_final = diretorio / f"{digest}.zip"
        try:
            # link cria o destino atomicamente e nunca sobrescreve conteúdo existente.
            os.link(caminho_temp, caminho_final)
            arquivo_criado = True
        except FileExistsError:
            hash_guardado, tamanho_guardado = _hash_arquivo(caminho_final)
            if hash_guardado != digest or tamanho_guardado != tamanho:
                raise RuntimeError("arquivo de conteúdo GTFS divergente no armazenamento")

        arquivo = ArquivoArmazenado(
            id=str(uuid4()), chave=chave, nome_original="feed.zip",
            mime_type="application/zip", tamanho_bytes=tamanho,
            sha256=digest, criado_em=agora,
        )
        versao = VersaoGTFS(
            id=str(uuid4()), capturada_em=agora, hash=digest,
            vigencia_inicio=inicio_feed or agora.astimezone(timezone.utc).date(),
            vigencia_fim=fim_feed, arquivo=arquivo,
        )
        try:
            db.session.add(arquivo)
            db.session.add(versao)
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            repetida = VersaoGTFS.query.filter_by(hash=digest).first()
            if repetida:
                return repetida
            raise
        return versao
    except Exception:
        db.session.rollback()
        # Remove arquivo órfão se a confirmação no banco falhou. Não toca em
        # conteúdo preexistente nem em uma versão já registrada.
        if arquivo_criado and caminho_final and not VersaoGTFS.query.filter_by(
            hash=caminho_final.stem
        ).first():
            caminho_final.unlink(missing_ok=True)
        raise
    finally:
        caminho_temp.unlink(missing_ok=True)
