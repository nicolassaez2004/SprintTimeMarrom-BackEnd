import json
from datetime import date, datetime, timedelta, timezone

import pyarrow.parquet as pq
import pytest

from app import create_app, db
from app.exportacoes.conferencia import conferir_parquet
from app.exportacoes.parquet import (
    _exportar,
    gerar_parquet_diario,
    nome_arquivo,
    prazo_publicacao,
)
from app.models import ExecucaoColeta, RegistroBrutoGPS

DIA = date(2026, 11, 19)
INSTANTE = datetime(2026, 11, 19, 10, tzinfo=timezone.utc)


@pytest.fixture
def contexto():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://"})
    with app.app_context():
        db.create_all()
        e = ExecucaoColeta(iniciada_em=INSTANTE, estado="OK", finalizada_em=INSTANTE)
        db.session.add(e)
        db.session.flush()
        db.session.add_all([
            RegistroBrutoGPS(execucao_id=e.id, coletado_em=INSTANTE, latitude=-23.500000001,
                             payload={"ausente": None, "objeto": {"b": 2, "a": 1}}),
            RegistroBrutoGPS(execucao_id=e.id, coletado_em=INSTANTE + timedelta(seconds=30),
                             payload=None),
        ])
        db.session.commit()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.mark.parametrize("desvio,esperado", [(0, True), (0.000001, False), (-1, True)])
def test_compara_conteudo_nulos_e_publicacao_no_limite(contexto, tmp_path, desvio, esperado):
    instante = prazo_publicacao(DIA) + timedelta(seconds=desvio)
    _exportar(tmp_path, DIA, lambda: instante, "TESTE_CONTROLADO")
    resultado = conferir_parquet(DIA, tmp_path / nome_arquivo(DIA), verificado_em=instante)
    assert resultado["conteudo_confere"] and resultado["metadados_conferem"]
    assert resultado["nulos_banco"] == resultado["nulos_parquet"]
    assert resultado["registros_banco"] == 2
    assert resultado["prazo_atendido"] is esperado
    assert resultado["aprovado"] is esperado


def test_geracao_nao_e_evidencia_de_publicacao(contexto, tmp_path):
    arquivo = tmp_path / nome_arquivo(DIA)
    gerar_parquet_diario(DIA, arquivo, relogio=lambda: prazo_publicacao(DIA))
    resultado = conferir_parquet(DIA, arquivo)
    assert resultado["conteudo_confere"]
    assert resultado["prazo_atendido"] is None
    assert resultado["aprovado"] is False


def test_payload_textual_preserva_literal_sem_interpretar_como_objeto_json(contexto, tmp_path):
    import pyarrow as pa

    registro = RegistroBrutoGPS.query.first()
    registro.payload = '{"texto":1}'
    db.session.commit()
    arquivo = tmp_path / nome_arquivo(DIA)
    _exportar(tmp_path, DIA, lambda: prazo_publicacao(DIA), "TESTE_CONTROLADO")
    assert conferir_parquet(DIA, arquivo)["conteudo_confere"] is True
    tabela = pq.read_table(arquivo)
    linhas = tabela.to_pylist()
    linhas[0]["payload"] = '{"texto": 1}'
    pq.write_table(pa.Table.from_pylist(linhas, schema=tabela.schema), arquivo)
    assert conferir_parquet(DIA, arquivo)["conteudo_confere"] is False


def test_modificacao_de_payload_e_metadados_nao_passam(contexto, tmp_path):
    instante = prazo_publicacao(DIA)
    _exportar(tmp_path, DIA, lambda: instante, "TESTE_CONTROLADO")
    arquivo = tmp_path / nome_arquivo(DIA)
    tabela = pq.read_table(arquivo)
    linhas = tabela.to_pylist()
    linhas[0]["payload"] = '{"modificado":true}'
    import pyarrow as pa
    pq.write_table(pa.Table.from_pylist(linhas, schema=tabela.schema), arquivo)
    resultado = conferir_parquet(DIA, arquivo)
    assert resultado["conteudo_confere"] is False
    assert resultado["prazo_atendido"] is None  # Hash não corresponde ao recibo.
    pq.write_table(tabela.replace_schema_metadata({b"license": b"outra"}), arquivo)
    assert conferir_parquet(DIA, arquivo)["metadados_conferem"] is False


def test_dado_tardio_e_recibo_sem_fuso_nao_produzem_falso_sucesso(contexto, tmp_path):
    instante = prazo_publicacao(DIA)
    _exportar(tmp_path, DIA, lambda: instante, "TESTE_CONTROLADO")
    arquivo = tmp_path / nome_arquivo(DIA)
    r = RegistroBrutoGPS.query.first()
    r.longitude = -46.6
    db.session.commit()
    assert conferir_parquet(DIA, arquivo)["conteudo_confere"] is False
    recibo = arquivo.with_suffix(".publicacao.json")
    dados = json.loads(recibo.read_text())
    dados["publicado_em"] = "2026-11-20T06:00:00"
    recibo.write_text(json.dumps(dados))
    assert conferir_parquet(DIA, arquivo)["prazo_atendido"] is None
