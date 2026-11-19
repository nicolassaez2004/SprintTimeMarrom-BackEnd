from datetime import date, datetime, timezone

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app import create_app, db
from app.exportacoes.parquet import gerar_parquet_diario
from app.models import ExecucaoColeta, RegistroBrutoGPS


@pytest.fixture
def app():
    application = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://"})
    with application.app_context():
        db.create_all()
        execucao = ExecucaoColeta(iniciada_em=datetime(2026, 11, 18, tzinfo=timezone.utc))
        db.session.add(execucao)
        db.session.flush()
        db.session.add_all([
            RegistroBrutoGPS(
                execucao_id=execucao.id, prefixo_origem="5800", latitude=-23.5,
                longitude=-46.6, horario_gps=datetime(2026, 11, 18, 10, tzinfo=timezone.utc),
                coletado_em=datetime(2026, 11, 18, 10, tzinfo=timezone.utc),
                payload={"p": "5800", "detalhes": {"a": 1}},
            ),
            RegistroBrutoGPS(
                execucao_id=execucao.id, prefixo_origem=None, latitude=None,
                longitude=None, horario_gps=None,
                coletado_em=datetime(2026, 11, 18, 23, 59, tzinfo=timezone.utc), payload=None,
            ),
            RegistroBrutoGPS(
                execucao_id=execucao.id, prefixo_origem="fora-do-dia",
                coletado_em=datetime(2026, 11, 19, 0, tzinfo=timezone.utc),
            ),
        ])
        db.session.commit()
        yield application
        db.session.remove()
        db.drop_all()


def test_exporta_dia_com_tipos_null_payload_e_metadados(app, tmp_path):
    destino = tmp_path / "gps.parquet"
    with app.app_context():
        resultado = gerar_parquet_diario(date(2026, 11, 18), destino, tamanho_lote=1)

    tabela = pq.read_table(destino)
    assert resultado["registros"] == 2
    assert tabela.num_rows == 2
    assert tabela.schema.field("latitude").type == pa.float64()
    assert tabela.column("latitude").null_count == 1
    assert tabela.column("payload")[0].as_py() == '{"p":"5800","detalhes":{"a":1}}'
    assert tabela.column("payload")[1].as_py() is None
    assert tabela.schema.metadata[b"license"] == b"CC BY 4.0"
    assert tabela.schema.metadata[b"date"] == b"2026-11-18"


def test_erro_na_consulta_nao_publica_arquivo_parcial(app, tmp_path, monkeypatch):
    destino = tmp_path / "gps.parquet"

    def erro(*args, **kwargs):
        raise RuntimeError("falha de leitura")

    with app.app_context():
        monkeypatch.setattr(db.engine, "connect", erro)
        with pytest.raises(RuntimeError, match="falha de leitura"):
            gerar_parquet_diario(date(2026, 11, 18), destino)

    assert not destino.exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("dia", [datetime(2026, 11, 18), "2026-11-18"])
def test_rejeita_data_que_nao_seja_date(dia, tmp_path):
    with pytest.raises(TypeError, match="datetime.date"):
        gerar_parquet_diario(dia, tmp_path / "gps.parquet")
