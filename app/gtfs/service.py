"""Captura, valida e versiona o ZIP original do GTFS por SHA-256."""

import csv
import os
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from flask import current_app
from sqlalchemy.exc import IntegrityError

from app import db
from app.integrations.gtfs import GTFSClient
from app.models import ArquivoArmazenado, VersaoGTFS
from app.storage import FileStore, hash_arquivo


class GTFSValidationError(ValueError):
    """O download terminou, mas não contém um feed GTFS íntegro."""


@dataclass(frozen=True)
class ResultadoCapturaGTFS:
    versao: VersaoGTFS
    nova: bool
    identificada_em: datetime
    persistida_em: datetime | None

    @property
    def tempo_ate_base_segundos(self):
        if self.persistida_em is None:
            return None
        return (self.persistida_em - self.identificada_em).total_seconds()


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


def capturar_gtfs(*, url=None, diretorio=None, cliente=None, agora=None):
    """Baixa e guarda uma captura imutável; hash repetido retorna a versão atual.

    O arquivo é validado antes de entrar no armazenamento permanente. A versão
    e os metadados do arquivo são confirmados em uma única transação SQL.
    """
    return capturar_gtfs_com_resultado(
        url=url, diretorio=diretorio, cliente=cliente, agora=agora,
    ).versao


def capturar_gtfs_com_resultado(*, url=None, diretorio=None, cliente=None,
                              agora=None, relogio=None, ao_identificar=None):
    """Inclui identificação do hash e confirmação do commit, sem alterar a API antiga.

    Identificação é o instante em que o ZIP válido foi baixado e teve seu hash
    calculado; não representa a data de publicação desconhecida da fonte.
    Uma repetição não tem novo instante de persistência.
    """
    relogio = relogio or (lambda: datetime.now(timezone.utc))
    url = url or current_app.config.get("GTFS_SOURCE_URL")
    diretorio = Path(diretorio or current_app.config.get("GTFS_STORAGE_DIR")
                     or (Path(current_app.instance_path) / "gtfs"))
    cliente = cliente or GTFSClient()
    agora = agora or relogio()
    if agora.tzinfo is None:
        raise ValueError("data de captura deve conter fuso horário")
    armazenamento = FileStore(diretorio)

    fd, temporario = tempfile.mkstemp(prefix="gtfs-", suffix=".part", dir=diretorio)
    os.close(fd)
    caminho_temp = Path(temporario)
    try:
        with caminho_temp.open("wb") as destino:
            cliente.baixar_para(url, destino)
            destino.flush()
            os.fsync(destino.fileno())

        inicio_feed, fim_feed = _validar_zip(caminho_temp)
        digest, tamanho = hash_arquivo(caminho_temp)
        identificada_em = relogio()
        if identificada_em.tzinfo is None:
            raise ValueError("data de identificação deve conter fuso horário")
        if ao_identificar is not None:
            # O agendador persiste a primeira identificação antes de publicar;
            # retomadas do mesmo hash não reiniciam a medição do prazo.
            identificada_em = ao_identificar(digest, identificada_em)
        # Publicar/verificar antes de reutilizar metadados, inclusive quando
        # outra máquina registrou o hash ou o ZIP local precisa ser recuperado.
        chave = armazenamento.publicar_gtfs(caminho_temp, digest, tamanho)
        existente = VersaoGTFS.query.filter_by(hash=digest).first()
        if existente:
            return ResultadoCapturaGTFS(existente, False, identificada_em, None)

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
                return ResultadoCapturaGTFS(repetida, False, identificada_em, None)
            raise
        return ResultadoCapturaGTFS(versao, True, identificada_em, relogio())
    except Exception:
        db.session.rollback()
        # O objeto final pode estar em uso por outra transação. Preservá-lo
        # permite retomada; somente o temporário privado é removido abaixo.
        raise
    finally:
        caminho_temp.unlink(missing_ok=True)
