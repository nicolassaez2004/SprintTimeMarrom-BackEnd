import json
import stat
from datetime import date, datetime, timedelta, timezone

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from sqlalchemy import event

from app import create_app, db
from app.exportacoes import parquet as exportador
from app.exportacoes.parquet import gerar_parquet_diario
from app.models import ExecucaoColeta, RegistroBrutoGPS

UTC = timezone.utc
DIA = date(2026, 11, 18)
# Dia de São Paulo (UTC-3): [2026-11-18T03:00Z, 2026-11-19T03:00Z).
INICIO = datetime(2026, 11, 18, 3, tzinfo=UTC)


@pytest.fixture
def app():
    application = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://"})
    with application.app_context():
        db.create_all()
        execucao = ExecucaoColeta(iniciada_em=datetime(2026, 11, 18, tzinfo=UTC))
        db.session.add(execucao)
        db.session.flush()

        def registro(coletado_em, **campos):
            return RegistroBrutoGPS(execucao_id=execucao.id, coletado_em=coletado_em, **campos)

        db.session.add_all([
            registro(INICIO - timedelta(microseconds=1), prefixo_origem="dia-anterior"),
            registro(INICIO, prefixo_origem="5800", latitude=-23.5, longitude=-46.6,
                     horario_gps=INICIO - timedelta(seconds=20),
                     payload={"p": "5800", "detalhes": {"a": 1}}),
            registro(INICIO + timedelta(days=1, microseconds=-1), prefixo_origem=None,
                     latitude=None, longitude=None, horario_gps=None, payload=None),
            registro(INICIO + timedelta(days=1), prefixo_origem="dia-seguinte"),
        ])
        db.session.commit()
        yield application
        db.session.remove()
        db.drop_all()


def _brutos_do_dia():
    return (RegistroBrutoGPS.query
            .filter(RegistroBrutoGPS.coletado_em >= INICIO,
                    RegistroBrutoGPS.coletado_em < INICIO + timedelta(days=1))
            .order_by(RegistroBrutoGPS.coletado_em, RegistroBrutoGPS.id).all())


def test_exporta_dia_de_sao_paulo_com_tipos_null_payload_e_metadados(app, tmp_path):
    destino = tmp_path / "gps.parquet"
    with app.app_context():
        resultado = gerar_parquet_diario(DIA, destino, tamanho_lote=1)

    tabela = pq.read_table(destino)
    assert resultado["registros"] == tabela.num_rows == 2
    assert tabela.schema.field("latitude").type == pa.float64()
    assert tabela.schema.field("coletado_em").type == pa.timestamp("us", tz="UTC")
    assert tabela.column("coletado_em").to_pylist() == [INICIO, INICIO + timedelta(days=1, microseconds=-1)]
    assert tabela.column("latitude").null_count == 1
    assert tabela.column("payload")[0].as_py() == '{"p":"5800","detalhes":{"a":1}}'
    assert tabela.column("payload")[1].as_py() is None
    metadados = {k.decode(): v.decode() for k, v in tabela.schema.metadata.items()}
    assert metadados["license"] == "CC BY 4.0"
    assert metadados["license_url"] == "https://creativecommons.org/licenses/by/4.0/"
    assert metadados["source_url"].startswith("https://api.olhovivo.sptrans.com.br/")
    assert metadados["date"] == "2026-11-18"
    assert metadados["period_start_utc"] == INICIO.isoformat()
    assert stat.S_IMODE(destino.stat().st_mode) == 0o644


def test_varios_lotes_correspondem_ao_banco_e_nao_alteram_brutos(app, tmp_path):
    destino = tmp_path / "gps.parquet"
    with app.app_context():
        execucao_id = ExecucaoColeta.query.one().id
        # Instantes repetidos exercitam o desempate por id na paginação.
        db.session.add_all([
            RegistroBrutoGPS(execucao_id=execucao_id, prefixo_origem=f"P{i}",
                             coletado_em=INICIO + timedelta(minutes=i // 3),
                             latitude=None if i % 4 == 0 else -23.5 + i * 1e-6,
                             payload=None if i % 5 == 0 else {"p": i, "nome": "ção"})
            for i in range(23)
        ])
        db.session.commit()
        esperado = _brutos_do_dia()
        antes = [(r.id, r.payload, r.latitude, r.coletado_em) for r in RegistroBrutoGPS.query.all()]
        comandos = []

        def registrar(conexao, cursor, comando, parametros, contexto, varios):
            comandos.append(comando.lstrip().split()[0].upper())

        event.listen(db.engine, "before_cursor_execute", registrar)
        try:
            resultado = gerar_parquet_diario(DIA, destino, tamanho_lote=4)
        finally:
            event.remove(db.engine, "before_cursor_execute", registrar)
        depois = [(r.id, r.payload, r.latitude, r.coletado_em) for r in RegistroBrutoGPS.query.all()]

    linhas = pq.read_table(destino).to_pylist()
    assert resultado["registros"] == len(linhas) == len(esperado) == 25
    assert set(comandos) == {"SELECT"} and antes == depois
    for bruto, linha in zip(esperado, linhas):
        for campo in ("id", "execucao_id", "prefixo_origem", "latitude", "longitude",
                      "horario_gps", "coletado_em"):
            assert linha[campo] == getattr(bruto, campo)
        assert (json.loads(linha["payload"]) if linha["payload"] else None) == bruto.payload


def test_falha_apos_lotes_gravados_mantem_publicacao_anterior(app, tmp_path, monkeypatch):
    destino = tmp_path / "gps.parquet"
    with app.app_context():
        gerar_parquet_diario(DIA, destino)
        anterior = destino.read_bytes()

        class FalhaNoSegundoLote(pq.ParquetWriter):
            lotes = 0

            def write_table(self, *args, **kwargs):
                FalhaNoSegundoLote.lotes += 1
                if FalhaNoSegundoLote.lotes == 2:
                    raise RuntimeError("falha no meio da escrita")
                return super().write_table(*args, **kwargs)

        monkeypatch.setattr(exportador.pq, "ParquetWriter", FalhaNoSegundoLote)
        with pytest.raises(RuntimeError, match="falha no meio"):
            gerar_parquet_diario(DIA, destino, tamanho_lote=1)

    assert destino.read_bytes() == anterior
    assert [p.name for p in tmp_path.iterdir()] == ["gps.parquet"]


def test_erro_na_consulta_nao_publica_arquivo_parcial(app, tmp_path, monkeypatch):
    destino = tmp_path / "gps.parquet"

    def erro(*args, **kwargs):
        raise RuntimeError("falha de leitura")

    with app.app_context():
        monkeypatch.setattr(db.engine, "connect", erro)
        with pytest.raises(RuntimeError, match="falha de leitura"):
            gerar_parquet_diario(DIA, destino)

    assert not destino.exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("dia", [datetime(2026, 11, 18), "2026-11-18"])
def test_rejeita_data_que_nao_seja_date(dia, tmp_path):
    with pytest.raises(TypeError, match="datetime.date"):
        gerar_parquet_diario(dia, tmp_path / "gps.parquet")
