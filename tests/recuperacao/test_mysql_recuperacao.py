"""Opt-in: US32_MYSQL_ADMIN_URL, exclusivamente MySQL local de ensaio."""

import hashlib
import json
import os
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from flask_migrate import upgrade
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app import create_app, db
from app.models import ArquivoArmazenado, ExecucaoColeta, RegistroBrutoGPS
from app.operacao import backup as service
from app.operacao.restauracao import restaurar_backup
from app.repositories import recuperacao as repo


@pytest.fixture
def ambiente(tmp_path):
    valor = os.environ.get("US32_MYSQL_ADMIN_URL")
    if not valor:
        pytest.skip("Exige US32_MYSQL_ADMIN_URL local explícita")
    url = make_url(valor)
    assert url.drivername == "mysql+pymysql" and url.host in {"127.0.0.1", "localhost"}
    codigo = uuid4().hex[:12]
    fonte = "us32_verificacao_" + codigo
    alvo = "marrom_restore_" + codigo
    engine = create_engine(url.set(database=None))
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
        c.execute(text(f"CREATE DATABASE `{fonte}` CHARACTER SET utf8mb4"))
    original = tmp_path / "originais"
    original.mkdir()
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": url.set(database=fonte),
                      "MYSQL_SSL_CA": None, "GTFS_STORAGE_DIR": str(original),
                      "MUNICIPIO_GEOJSON": None})
    try:
        with app.app_context():
            upgrade()
            agora = datetime(2026, 11, 19, 15, 30, 12, 123456, tzinfo=timezone.utc)
            ciclo = ExecucaoColeta(iniciada_em=agora, estado="OK", finalizada_em=agora)
            db.session.add(ciclo)
            db.session.flush()
            db.session.add_all([
                RegistroBrutoGPS(execucao_id=ciclo.id, coletado_em=agora, latitude=-23.123456789,
                                 payload={"objeto": [1, None, "acentuação"]}),
                RegistroBrutoGPS(execucao_id=ciclo.id, coletado_em=agora, payload=None),
            ])
            conteudo = b"original GTFS imutavel\x00\xff"
            digest = hashlib.sha256(conteudo).hexdigest()
            (original / (digest + ".zip")).write_bytes(conteudo)
            db.session.add(ArquivoArmazenado(chave=f"gtfs/{digest}.zip", nome_original="GTFS.zip",
                                           mime_type="application/zip", tamanho_bytes=len(conteudo), sha256=digest))
            db.session.commit()
            with db.engine.begin() as c:
                c.execute(text("CREATE TABLE tipos_ensaio (id INT PRIMARY KEY, "
                               "trajeto LINESTRING, valor DECIMAL(20,8), binario BLOB, duracao TIME(6)) ENGINE=InnoDB"))
                c.execute(text("INSERT INTO tipos_ensaio VALUES (1,ST_GeomFromText("
                               "'LINESTRING(-46.6 -23.5,-46.7 -23.6)'),123456789.12345678,"
                               "0x00FFAA,'-23:05:06.123456')"))
            yield app, url.set(database=alvo), tmp_path
    finally:
        with app.app_context():
            db.session.remove()
            db.engine.dispose()
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
            # Somente os dois nomes UUID criados por esta fixture.
            assert fonte.startswith("us32_verificacao_") and alvo.startswith("marrom_restore_")
            c.execute(text(f"DROP DATABASE IF EXISTS `{alvo}`"))
            c.execute(text(f"DROP DATABASE `{fonte}`"))
        engine.dispose()


def test_backup_completo_restaura_snapshot_ddl_tipos_arquivos_e_aplicacao(ambiente, monkeypatch):
    app, url, pasta = ambiente
    with app.app_context():
        # Aiven usa ANSI_QUOTES; restaurar no MySQL local deve preservar o modo.
        from sqlalchemy import event
        @event.listens_for(db.engine, "checkout")
        def modo_ansi(conn, *_):
            with conn.cursor() as c:
                c.execute("SET SESSION sql_mode='ANSI,STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION'")
        original_linhas = repo.linhas
        inseriu = False

        def linhas_com_concorrencia(conn, tabela):
            nonlocal inseriu
            yield from original_linhas(conn, tabela)
            if not inseriu:
                inseriu = True
                with db.engine.begin() as outra:
                    outra.execute(text("INSERT INTO registros_gps (execucao_id,coletado_em,payload) "
                                       "SELECT id,'2026-11-19 15:31:00',NULL FROM execucoes_coleta LIMIT 1"))
        monkeypatch.setattr(repo, "linhas", linhas_com_concorrencia)
        resultado = service.criar_backup(pasta / "backups")
        monkeypatch.setattr(repo, "linhas", original_linhas)
        manifesto, esquema = service.validar_conjunto(resultado["diretorio"])
        assert "ANSI_QUOTES" in esquema["sql_mode"]
        assert all("ENGINE=InnoDB" in t["ddl"] for t in esquema["tabelas"])
        assert manifesto["originais"] == 1
        assert next(t for t in esquema["tabelas"] if t["nome"] == "registros_gps")["registros"] == 2
        assert db.session.query(RegistroBrutoGPS).count() == 3
    restaurado = restaurar_backup(resultado["diretorio"], url, pasta / "restaurado")
    assert restaurado["tabelas"]["registros_gps"]["registros"] == 2
    assert restaurado["tabelas"]["tipos_ensaio"]["registros"] == 1
    assert restaurado["conexao_aplicacao"] and restaurado["hashes_conferidos"]
    assert restaurado["chaves_estrangeiras_conferidas"] > 0
    assert restaurado["ate_24_horas"]
    with pytest.raises(ValueError, match="já existe"):
        restaurar_backup(resultado["diretorio"], url, pasta / "outra-tentativa")


def test_falha_de_original_nao_publica_backup_com_sucesso(ambiente):
    app, _, pasta = ambiente
    with app.app_context():
        for caminho in Path(app.config["GTFS_STORAGE_DIR"]).iterdir():
            caminho.write_bytes(b"corrompido")
        with pytest.raises(ValueError, match="divergente"):
            service.criar_backup(pasta / "backups")
    assert not list((pasta / "backups").glob("backup-*/manifesto.json"))
    falha = next((pasta / "backups").glob("*.falha.json"))
    assert json.loads(falha.read_text())["backup_publicado"] is False


def test_detecta_orfaos_mesmo_apos_religar_foreign_key_checks(ambiente):
    app, _, _ = ambiente
    with app.app_context(), closing(db.engine.raw_connection()) as conn:
        with conn.cursor() as c:
            c.execute("SET FOREIGN_KEY_CHECKS=0")
            c.execute("INSERT INTO registros_gps (execucao_id,coletado_em) VALUES (999999,'2026-11-19')")
            c.execute("SET FOREIGN_KEY_CHECKS=1")
        with pytest.raises(ValueError, match="órfãs"):
            repo.conferir_relacionamentos(conn, repo.metadados(conn)["tabelas"])
        conn.rollback()
