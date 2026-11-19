"""US05-T01: catalogação de PDFs oficiais. PDFs e metadados são sintéticos."""

import importlib.util
import io
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError

from app import create_app, db
from app.documentos import __main__ as cli
from app.documentos.service import caminho_original, catalogar_documento, consultar_documento
from app.models import ArquivoArmazenado, DocumentoOficial

UTC = timezone.utc
AGORA = datetime(2026, 11, 19, 12, tzinfo=UTC)
PDF = b"%PDF-1.4\n% documento sintetico de teste\n%%EOF\n"


@pytest.fixture
def app(tmp_path):
    aplicacao = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://",
                            "MYSQL_SSL_CA": None, "DOCUMENTOS_STORAGE_DIR": None})
    aplicacao.instance_path = str(tmp_path / "instance")
    with aplicacao.app_context():
        event.listen(db.engine, "connect", lambda conexao, _: conexao.execute("PRAGMA foreign_keys=ON"))
        db.create_all()
        yield aplicacao
        db.session.remove()
        db.engine.dispose()


@pytest.fixture
def pdf(tmp_path):
    caminho = tmp_path / "entrada" / "documento-sintetico.pdf"
    caminho.parent.mkdir()
    caminho.write_bytes(PDF)
    return caminho


def metadados(**extra):
    base = dict(titulo="Documento sintético de teste", tipo="CONTRATO",
                data_documento=date(2026, 10, 1), orgao="Órgão sintético",
                fonte="https://exemplo.invalid/documento.pdf",
                descoberto_em=AGORA - timedelta(days=2), agora=AGORA)
    base.update(extra)
    return base


def test_cadastro_guarda_pdf_e_metadados(app, pdf):
    documento, criado = catalogar_documento(pdf, **metadados())
    assert criado
    salvo = db.session.get(DocumentoOficial, documento.id)
    assert (salvo.titulo, salvo.tipo, salvo.orgao) == ("Documento sintético de teste", "CONTRATO", "Órgão sintético")
    assert salvo.data_documento == date(2026, 10, 1)
    assert salvo.catalogado_em == AGORA and salvo.descoberto_em == AGORA - timedelta(days=2)
    arquivo = salvo.arquivo
    assert arquivo.mime_type == "application/pdf" and arquivo.nome_original == "documento-sintetico.pdf"
    assert arquivo.tamanho_bytes == len(PDF) and arquivo.chave == f"documentos/{arquivo.sha256}.pdf"


def test_consulta_retorna_metadados_procedencia_e_prazo(app, pdf):
    documento, _ = catalogar_documento(pdf, **metadados())
    consulta = consultar_documento(documento.id)
    assert consulta["fonte"] == "https://exemplo.invalid/documento.pdf"
    assert consulta["data_documento"] == "2026-10-01"
    assert consulta["arquivo"]["sha256"] == documento.arquivo.sha256
    assert consulta["prazo"] == {"limite_dias": 30, "horas_ate_catalogacao": 48.0, "cumprido": True}
    with pytest.raises(LookupError):
        consultar_documento("inexistente")


def test_recupera_pdf_original_identico_e_detecta_adulteracao(app, pdf):
    documento, _ = catalogar_documento(pdf, **metadados())
    pdf.unlink()  # a recuperação não depende do arquivo de entrada
    caminho = caminho_original(documento.id)
    assert caminho.read_bytes() == PDF
    caminho.write_bytes(PDF + b"alterado")
    with pytest.raises(FileNotFoundError):
        caminho_original(documento.id)


@pytest.mark.parametrize("dias, cumprido", [(30, True), (30.0001, False), (45, False)])
def test_prazo_de_catalogacao_de_30_dias(app, pdf, dias, cumprido):
    documento, _ = catalogar_documento(pdf, **metadados(descoberto_em=AGORA - timedelta(days=dias)))
    assert documento.catalogado_no_prazo is cumprido
    assert consultar_documento(documento.id)["prazo"]["cumprido"] is cumprido


def test_repeticao_nao_duplica_e_mesmo_pdf_pode_ter_outro_documento(app, pdf):
    primeiro, _ = catalogar_documento(pdf, **metadados())
    repetido, criado = catalogar_documento(pdf, **metadados())
    assert not criado and repetido.id == primeiro.id
    outro, criado = catalogar_documento(pdf, **metadados(tipo="INDICADOR_OFICIAL"))
    assert criado and outro.arquivo_id == primeiro.arquivo_id
    assert ArquivoArmazenado.query.count() == 1 and DocumentoOficial.query.count() == 2


def test_integridade_dos_vinculos(app, pdf):
    documento, _ = catalogar_documento(pdf, **metadados())
    db.session.delete(documento.arquivo)
    with pytest.raises(IntegrityError):  # RESTRICT: o PDF referenciado não pode sumir
        db.session.commit()
    db.session.rollback()
    db.session.add(DocumentoOficial(titulo="t", tipo="t", data_documento=date(2026, 1, 1), orgao="o",
                                    fonte="f", descoberto_em=AGORA, catalogado_em=AGORA,
                                    arquivo_id="sem-arquivo"))
    with pytest.raises(IntegrityError):  # documento órfão
        db.session.commit()
    db.session.rollback()
    db.session.add(DocumentoOficial(titulo="t", tipo="t", data_documento=date(2026, 1, 1), orgao="o",
                                    fonte="f", descoberto_em=AGORA, catalogado_em=AGORA - timedelta(seconds=1),
                                    arquivo_id=documento.arquivo_id))
    with pytest.raises(IntegrityError):  # catalogação anterior à descoberta
        db.session.commit()
    db.session.rollback()


@pytest.mark.parametrize("extra", [
    {"titulo": "  "}, {"orgao": ""}, {"fonte": None},
    {"descoberto_em": datetime(2026, 11, 1)},  # sem fuso
    {"descoberto_em": AGORA + timedelta(minutes=1)},
    {"data_documento": "2026-10-01"},
])
def test_rejeita_metadados_invalidos_sem_gravar(app, pdf, extra):
    with pytest.raises(ValueError):
        catalogar_documento(pdf, **metadados(**extra))
    assert DocumentoOficial.query.count() == ArquivoArmazenado.query.count() == 0


def test_rejeita_arquivo_que_nao_e_pdf(app, tmp_path):
    falso = tmp_path / "falso.pdf"
    falso.write_bytes(b"PK\x03\x04 nao e pdf")
    with pytest.raises(ValueError):
        catalogar_documento(falso, **metadados())
    assert ArquivoArmazenado.query.count() == 0
    assert not list((Path(app.instance_path) / "documentos").glob("*"))  # nada publicado


def test_cli_cataloga_consulta_e_recupera_original(app, pdf, monkeypatch, capsys):
    monkeypatch.setattr(cli, "create_app", lambda: app)
    assert cli.main(["catalogar", "--pdf", str(pdf), "--titulo", "Documento sintético", "--tipo", "CONTRATO",
                     "--data-documento", "2026-10-01", "--orgao", "Órgão sintético",
                     "--fonte", "https://exemplo.invalid/a.pdf",
                     "--descoberto-em", "2026-11-18T10:00-03:00"]) == 0
    cadastro = json.loads(capsys.readouterr().out)
    assert cadastro["estado"] == "CATALOGADO" and cadastro["prazo"]["cumprido"] is True
    assert cli.main(["consultar", "--id", cadastro["id"]]) == 0
    assert json.loads(capsys.readouterr().out)["arquivo"] == cadastro["arquivo"]
    assert cli.main(["original", "--id", cadastro["id"]]) == 0
    assert Path(json.loads(capsys.readouterr().out)["caminho"]).read_bytes() == PDF
    assert cli.main(["consultar", "--id", "inexistente"]) == 1
    assert json.loads(capsys.readouterr().out)["estado"] == "ERRO"


def test_migracao_us05_corresponde_ao_modelo_e_gera_ddl_mysql_sem_executar(app):
    caminho = Path(__file__).resolve().parents[2] / "migrations/versions/us05_0014_documentos_oficiais.py"
    spec = importlib.util.spec_from_file_location("migracao_us05", caminho)
    migracao = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migracao)
    assert migracao.down_revision == "us09_0013"
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as conexao:
            antigas = [t for t in db.metadata.sorted_tables if t.name != "documentos_oficiais"]
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
        assert "CREATE TABLE documentos_oficiais" in sql and "DATETIME(6)" in sql
        assert "REFERENCES arquivos_armazenados (id) ON DELETE RESTRICT" in sql
        assert "catalogado_em >= descoberto_em" in sql
    finally:
        engine.dispose()
