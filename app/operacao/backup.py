"""BackupService: banco + FileStore, publicação somente após todos os hashes."""

import hashlib
import json
import os
import shutil
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from uuid import uuid4

from filelock import FileLock
from flask import current_app

from app import db
from app.operacao.arquivos import caminho_interno, gravar_json
from app.repositories import recuperacao as repo
from app.storage import hash_arquivo


def raizes_arquivos():
    return {nome: Path(current_app.config.get(config) or Path(current_app.instance_path) / nome)
            for nome, config in (("gtfs", "GTFS_STORAGE_DIR"),
                                 ("programacao", "PROGRAMACAO_STORAGE_DIR"),
                                 ("documentos", "DOCUMENTOS_STORAGE_DIR"))}


def validar_destino(destino, raizes):
    destino = Path(destino).resolve()
    protegidos = [Path(current_app.root_path).parent.resolve(), *[p.resolve() for p in raizes.values()]]
    if any(destino.is_relative_to(p) or p.is_relative_to(destino) for p in protegidos):
        raise ValueError("Backup deve ficar fora do repositório e dos originais")
    return destino


def _copiar_origem(raizes, registro, pasta):
    chave = PurePosixPath(registro["chave"])
    if len(chave.parts) != 2 or chave.parts[0] not in raizes:
        raise ValueError("Chave FileStore desconhecida; backup recusado")
    origem = caminho_interno(raizes[chave.parts[0]], chave.parts[1])
    destino = caminho_interno(pasta / "originais", chave.as_posix())
    destino.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(origem, destino)
    if hash_arquivo(destino) != (registro["sha256"], registro["tamanho_bytes"]):
        raise ValueError("Original ausente ou divergente; backup não pode ser concluído")


def criar_backup(destino):
    if db.engine.dialect.name != "mysql":
        raise ValueError("Backup operacional exige MySQL")
    raizes = raizes_arquivos()
    destino = validar_destino(destino, raizes)
    destino.mkdir(parents=True, exist_ok=True)
    with FileLock(str(destino / ".backup.lock"), timeout=0):
        return _criar(destino, raizes)


def _criar(destino, raizes):
    iniciada = datetime.now(timezone.utc)
    nome = iniciada.strftime("backup-%Y%m%dT%H%M%SZ-") + uuid4().hex[:8]
    parcial = destino / ("." + nome + ".partial")
    parcial.mkdir()
    try:
        with closing(db.engine.raw_connection()) as conn:
            with conn.cursor() as c:
                c.execute("SET SESSION time_zone='+00:00'")
                c.execute("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                c.execute("SET SESSION lock_wait_timeout=15")
                c.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
            esquema = repo.metadados(conn)
            if "alembic_version" not in {t["nome"] for t in esquema["tabelas"]}:
                raise ValueError("Base sem revisão Alembic; recuperação não rastreável")
            (parcial / "dados").mkdir()
            arquivos = []
            for tabela in esquema["tabelas"]:
                digest, quantidade = hashlib.sha256(), 0
                tabela["dados"] = f"dados/{tabela['nome']}.jsonl"
                with caminho_interno(parcial, tabela["dados"]).open("wb") as stream:
                    for linha in repo.linhas(conn, tabela):
                        bloco = repo.serializar(linha)
                        stream.write(bloco)
                        digest.update(bloco)
                        quantidade += 1
                        if tabela["nome"] == "arquivos_armazenados":
                            arquivos.append(dict(zip(tabela["colunas"], linha, strict=True)))
                    stream.flush()
                    os.fsync(stream.fileno())
                tabela.update(registros=quantidade, sha256_linhas=digest.hexdigest())
            # A lista pertence ao mesmo snapshot dos demais dados. Os objetos
            # são imutáveis; arquivos de novas capturas não entram por acidente.
            for arquivo in arquivos:
                _copiar_origem(raizes, arquivo, parcial)
            repo.conferir_relacionamentos(conn, esquema["tabelas"])
            # MDL impede DDL nas tabelas lidas; também detectar novas tabelas.
            segunda = repo.metadados(conn)
            if [(t["nome"], repo.esquema_normalizado(t["ddl"])) for t in esquema["tabelas"]] != [
                (t["nome"], repo.esquema_normalizado(t["ddl"])) for t in segunda["tabelas"]
            ]:
                raise ValueError("Esquema alterado durante o backup")
            conn.rollback()  # Transação somente de leitura.
        gravar_json(parcial / "esquema.json", esquema)
        # Configuração suficiente para reconstruir os caminhos; segredos ficam
        # no gerenciador/.env da equipe e nunca são copiados para o pacote.
        configuracao = {
            "GTFS_STORAGE_DIR": "originais/gtfs", "PROGRAMACAO_STORAGE_DIR": "originais/programacao",
            "DOCUMENTOS_STORAGE_DIR": "originais/documentos", "EXPORTACOES_DIR": "exportacoes",
            "COLETA_STATE_DIR": "coleta-nova", "GPS_COLLECTION_INTERVAL_SECONDS":
                current_app.config["GPS_COLLECTION_INTERVAL_SECONDS"],
            "segredos_necessarios_externos": ["DATABASE_URL", "SPTRANS_TOKEN"],
            "retomada": "Não iniciar workers durante o ensaio. Usar checkpoint novo na restauração; "
                        "preservar brutos e revisar execuções interrompidas antes de retomar a coleta.",
        }
        limite = current_app.config.get("MUNICIPIO_GEOJSON")
        if limite:
            alvo = parcial / "configuracao" / "municipio.geojson"
            alvo.parent.mkdir()
            shutil.copyfile(limite, alvo)
            configuracao["MUNICIPIO_GEOJSON"] = "configuracao/municipio.geojson"
        gravar_json(parcial / "configuracao.json", configuracao)
        lista = {}
        for caminho in parcial.rglob("*"):
            if caminho.is_file():
                with caminho.open("r+b") as stream:
                    os.fsync(stream.fileno())
                digest, tamanho = hash_arquivo(caminho)
                lista[caminho.relative_to(parcial).as_posix()] = {"sha256": digest, "bytes": tamanho}
        manifesto = {"formato": "time-marrom-mysql-jsonl-v2", "estado": "CONCLUIDO",
                     "iniciada_em": iniciada.isoformat(),
                     "concluida_em": datetime.now(timezone.utc).isoformat(),
                     "mysql_versao": esquema["mysql_versao"], "banco_origem": esquema["banco"],
                     "tabelas": len(esquema["tabelas"]), "originais": len(arquivos), "arquivos": lista,
                     "snapshot": "REPEATABLE READ / WITH CONSISTENT SNAPSHOT / READ ONLY / InnoDB",
                     "segredos_incluidos": False, "restauracao_testada": False}
        gravar_json(parcial / "manifesto.json", manifesto)
        with (parcial / "manifesto.sha256").open("w", encoding="ascii") as stream:
            stream.write(hash_arquivo(parcial / "manifesto.json")[0])
            stream.flush()
            os.fsync(stream.fileno())
        final = destino / nome
        os.replace(parcial, final)
        return {"estado": "CONCLUIDO", "diretorio": str(final), **manifesto}
    except BaseException as erro:
        # O parcial permanece para diagnóstico; nunca é candidato a restauração.
        gravar_json(destino / (nome + ".falha.json"), {
            "estado": "FALHA", "iniciada_em": iniciada.isoformat(),
            "tipo_erro": type(erro).__name__, "backup_publicado": False,
        })
        raise


def validar_conjunto(pasta):
    pasta = Path(pasta).resolve()
    if pasta.name.endswith(".partial"):
        raise ValueError("Backup parcial não pode ser restaurado")
    esperado = (pasta / "manifesto.sha256").read_text(encoding="ascii").strip()
    if hash_arquivo(pasta / "manifesto.json")[0] != esperado:
        raise ValueError("Manifesto divergente")
    manifesto = json.loads((pasta / "manifesto.json").read_text(encoding="utf-8"))
    if manifesto["estado"] != "CONCLUIDO" or manifesto["formato"] != "time-marrom-mysql-jsonl-v2":
        raise ValueError("Formato ou estado de backup inválido")
    for nome, info in manifesto["arquivos"].items():
        if hash_arquivo(caminho_interno(pasta, nome)) != (info["sha256"], info["bytes"]):
            raise ValueError("Arquivo do backup corrompido")
    if not {"esquema.json", "configuracao.json"}.issubset(manifesto["arquivos"]):
        raise ValueError("Backup sem esquema/configuração")
    esquema = json.loads((pasta / "esquema.json").read_text(encoding="utf-8"))
    if not isinstance(esquema.get("sql_mode"), str) or esquema.get("ddl_sql_mode") != repo.DDL_SQL_MODE:
        raise ValueError("Backup não registra sql_mode necessário à recuperação do DDL")
    for tabela in esquema["tabelas"]:
        if tabela["dados"] not in manifesto["arquivos"]:
            raise ValueError("Tabela sem arquivo de dados")
    return manifesto, esquema
