"""Opt-in (US28-T01): somente base MySQL vazia e descartável us28_verificacao_*.

Aplica a cadeia Alembic inteira, confere constraints de ``pedidos_extracao`` e
dispara pedidos idênticos concorrentes. Nunca usa DATABASE_URL das bases do time.
"""
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from flask_migrate import upgrade
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from app import create_app, db
from app.models import ExecucaoProcessamento, PedidoExtracao

CORPO = {"linha": "33887", "inicio": "2026-11-01T00:00:00-03:00",
         "fim": "2026-11-08T00:00:00-03:00", "formato": "CSV"}


def test_pedidos_extracao_no_mysql():
    valor = os.getenv("US28_MYSQL_TEST_URL")
    if not valor:
        pytest.skip("Configure US28_MYSQL_TEST_URL para integração MySQL isolada")
    url = make_url(valor)
    assert url.get_backend_name() == "mysql"
    assert url.database.startswith("us28_verificacao_"), "Recusa base de uso do time"
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": url, "MYSQL_SSL_CA": None})
    migrations = str(Path(__file__).resolve().parents[1] / "migrations")
    with app.app_context():
        try:
            assert not inspect(db.engine).get_table_names(), "Exige base vazia"
            upgrade(directory=migrations)
            barreira = Barrier(8)

            def pedir(_):
                cliente = app.test_client()
                barreira.wait()
                resposta = cliente.post("/api/extracoes", json=CORPO)
                return resposta.status_code, resposta.get_json()["id"]

            with ThreadPoolExecutor(8) as executor:
                respostas = list(executor.map(pedir, range(8)))
            assert {s for s, _ in respostas} == {202}
            assert len({i for _, i in respostas}) == 1
            assert (PedidoExtracao.query.count(), ExecucaoProcessamento.query.count()) == (1, 1)
            pedido_id = respostas[0][1]
            assert app.test_client().get(f"/api/extracoes/{pedido_id}").get_json()["estado"] == "AGUARDANDO"

            with db.engine.connect() as conexao:
                assert conexao.execute(text(
                    "SELECT ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE()"
                    " AND TABLE_NAME='pedidos_extracao'")).scalar_one() == "InnoDB"
            violacoes = {
                # job já tem pedido (UNIQUE)
                1062: "INSERT INTO pedidos_extracao SELECT 'dup',linha_codigo,inicio_periodo,fim_periodo,"
                      "formato,criado_em,versao_base,execucao_processamento_id,NULL FROM pedidos_extracao",
                # job inexistente (FK)
                1452: "INSERT INTO pedidos_extracao VALUES ('fk','1','2026-11-01','2026-11-02','CSV',"
                      "'2026-11-01','b','nao-existe',NULL)",
                # formato fora do domínio (CHECK)
                3819: "UPDATE pedidos_extracao SET formato='XLSX'",
                # apagar job com pedido (RESTRICT)
                1451: "DELETE FROM execucoes_processamento",
            }
            for codigo, sql in violacoes.items():
                with pytest.raises(DBAPIError) as erro:
                    with db.engine.begin() as conexao:
                        conexao.execute(text(sql))
                assert erro.value.orig.args[0] == codigo, sql
        finally:
            db.session.remove()
            db.engine.dispose()


def test_geracao_de_extracao_no_mysql(tmp_path):
    """US28-T02: worker com leitura REPEATABLE READ, progresso, FOR UPDATE e perda de posse."""
    from datetime import datetime, timedelta, timezone

    import pyarrow.parquet as pq

    from app.exportacoes.extracoes import ExportService, TIPO, armazenamento, executor_extracao
    from app.jobs.executor import Executor
    from app.models import ArquivoArmazenado, ExecucaoColeta, RegistroBrutoGPS

    valor = os.getenv("US28_T02_MYSQL_TEST_URL")
    if not valor:
        pytest.skip("Configure US28_T02_MYSQL_TEST_URL para integração MySQL isolada")
    url = make_url(valor)
    assert url.get_backend_name() == "mysql"
    assert url.database.startswith("us28_verificacao_"), "Recusa base de uso do time"
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": url, "MYSQL_SSL_CA": None,
                      "EXPORTACOES_DIR": str(tmp_path)})
    migrations = str(Path(__file__).resolve().parents[1] / "migrations")
    inicio = datetime(2026, 11, 1, 3, tzinfo=timezone.utc)
    with app.app_context():
        try:
            assert not inspect(db.engine).get_table_names(), "Exige base vazia"
            upgrade(directory=migrations)
            ciclo = ExecucaoColeta(iniciada_em=inicio)
            db.session.add(ciclo)
            db.session.flush()
            for minutos in range(9):
                for linha in ("33887", "1111"):
                    db.session.add(RegistroBrutoGPS(execucao_id=ciclo.id, linha_origem=linha,
                                                    coletado_em=inicio + timedelta(minutes=minutos)))
            db.session.commit()
            cliente = app.test_client()
            ids = [cliente.post("/api/extracoes", json={**CORPO, "formato": f}).get_json()["id"]
                   for f in ("PARQUET", "CSV")]

            assert executor_extracao(app, ExportService(tamanho_lote=2)).executar_um()[1] == "PRONTO"
            # Segundo pedido: o worker antigo perde a posse antes de publicar.
            servico, depois = ExportService(tamanho_lote=2), lambda: datetime.now(timezone.utc) + timedelta(hours=1)
            novo = []

            def gerar_e_perder(job):
                gerado = servico.gerar(job)
                novo.append(executor_extracao(app, ExportService(tamanho_lote=3), relogio=depois).executar_um())
                return gerado
            assert Executor(app, TIPO, gerar_e_perder, servico.publicar).executar_um()[1] == "ERRO"
            assert novo[0][1] == "PRONTO"

            # As requisições reutilizam este app context: encerra o snapshot
            # REPEATABLE READ aberto pelo POST, como o teardown faria por requisição.
            db.session.remove()
            for pedido_id in ids:
                corpo = cliente.get(f"/api/extracoes/{pedido_id}").get_json()
                assert corpo["estado"] == "PRONTO" and corpo["progresso"]["registros"] == 9
            arquivo = ArquivoArmazenado.query.filter_by(mime_type="application/vnd.apache.parquet").one()
            caminho = armazenamento().caminho_extracao(arquivo.chave, arquivo.sha256)
            assert set(pq.read_table(caminho).column("linha_origem").to_pylist()) == {"33887"}
            assert ArquivoArmazenado.query.count() == 2
            assert not list(Path(tmp_path, "extracoes").glob(".extracao-*.tmp"))
        finally:
            db.session.remove()
            db.engine.dispose()
