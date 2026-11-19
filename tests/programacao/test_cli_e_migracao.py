import importlib.util
import io
import json
from pathlib import Path
from unittest.mock import Mock

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine

from app import db
from app.integrations.programacao import FonteOficialIndisponivel
from app.programacao import __main__ as cli
from app.programacao import service

ARGUMENTOS = ["--linha", "3762-10", "--sentido", "ida", "--data", "2026-11-19", "--tipo-dia", "UTIL"]


def test_comandos_importam_arquivo_e_recuperam_procedencia(versao, fabrica_captura, app, tmp_path, monkeypatch, capsys):
    captura = fabrica_captura()
    arquivo = tmp_path / "captura-sintetica.json"
    arquivo.write_bytes(captura.conteudo)
    monkeypatch.setattr(cli, "create_app", lambda: app)
    assert cli.main(["importar", *ARGUMENTOS, "--arquivo-json", str(arquivo),
                     "--fonte-url", captura.fonte_url]) == 0
    resultado = json.loads(capsys.readouterr().out)
    assert resultado["estado"] == "IMPORTADA"
    assert cli.main(["consultar", *ARGUMENTOS]) == 0
    consulta = json.loads(capsys.readouterr().out)
    assert consulta["procedencia"] == resultado["procedencia"]
    assert consulta["quantidade"] == 3


def test_cli_sinaliza_fallback_sem_sucesso_falso(versao, app, monkeypatch, capsys):
    cliente = Mock()
    cliente.obter.side_effect = FonteOficialIndisponivel("HTTP_403")
    monkeypatch.setattr(service, "SPTransProgrammingClient", lambda: cliente)
    monkeypatch.setattr(cli, "create_app", lambda: app)
    assert cli.main(["importar", *ARGUMENTOS]) == 2
    resultado = json.loads(capsys.readouterr().out)
    assert resultado["motivo"] == "HTTP_403" and resultado["oficial_disponivel"] is False


def test_migracao_0008_corresponde_ao_modelo_e_gera_ddl_mysql_sem_executar(app):
    caminho = Path(__file__).resolve().parents[2] / "migrations/versions/0008_programacao_oficial.py"
    spec = importlib.util.spec_from_file_location("migracao_programacao", caminho)
    migracao = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migracao)
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as conexao:
            antigas = [tabela for tabela in db.metadata.sorted_tables
                       if tabela.name not in ("importacoes_programacao", "partidas_programadas")]
            db.metadata.create_all(conexao, tables=antigas)
            contexto = MigrationContext.configure(conexao)
            with Operations.context(contexto):
                migracao.upgrade()
            assert compare_metadata(contexto, db.metadata) == []
        saida = io.StringIO()
        offline = MigrationContext.configure(dialect_name="mysql", opts={"as_sql": True, "output_buffer": saida})
        with Operations.context(offline):
            migracao.upgrade()
        sql = saida.getvalue()
        assert "CREATE TABLE importacoes_programacao" in sql
        assert "CREATE TABLE partidas_programadas" in sql
        assert "DATETIME(6)" in sql and "utf8mb4_0900_bin" in sql
        assert "REFERENCES linhas (versao_gtfs_id, codigo)" in sql
        assert "intervalo_seg IS NOT NULL" in sql
    finally:
        engine.dispose()
