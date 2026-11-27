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
            assert app.test_client().get(f"/api/extracoes/{pedido_id}").get_json()["estado"] == "aguardando"

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
                assert corpo["estado"] == "pronto" and corpo["progresso"]["registros"] == 9
            arquivo = ArquivoArmazenado.query.filter_by(mime_type="application/vnd.apache.parquet").one()
            caminho = armazenamento().caminho_extracao(arquivo.chave, arquivo.sha256)
            assert set(pq.read_table(caminho).column("linha_origem").to_pylist()) == {"33887"}
            assert ArquivoArmazenado.query.count() == 2
            assert not list(Path(tmp_path, "extracoes").glob(".extracao-*.tmp"))
        finally:
            db.session.remove()
            db.engine.dispose()


def _mysql_isolado(variavel, tmp_path):
    """App sobre base MySQL nova ``<base us28_verificacao_*>_<n>``, criada para o
    teste no servidor isolado, com a cadeia Alembic inteira aplicada."""
    from sqlalchemy import create_engine

    valor = os.getenv(variavel)
    if not valor:
        pytest.skip(f"Configure {variavel} para integração MySQL isolada")
    url = make_url(valor)
    assert url.get_backend_name() == "mysql"
    assert url.database.startswith("us28_verificacao_"), "Recusa base de uso do time"
    url = url.set(database=f"{url.database}_{abs(hash(tmp_path.name)) % 10**8}")
    servidor = create_engine(url.set(database="information_schema"))
    with servidor.begin() as conexao:
        conexao.execute(text(f"DROP DATABASE IF EXISTS `{url.database}`"))
        conexao.execute(text(f"CREATE DATABASE `{url.database}`"))
    servidor.dispose()
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": url, "MYSQL_SSL_CA": None,
                      "EXPORTACOES_DIR": str(tmp_path)})
    with app.app_context():
        assert not inspect(db.engine).get_table_names(), "Exige base vazia"
        upgrade(directory=str(Path(__file__).resolve().parents[1] / "migrations"))
    return app


def test_limpeza_nao_apaga_temporario_de_job_reservado_por_outro_worker(tmp_path):
    """Review US28: a posse é lida num snapshot novo, não no REPEATABLE READ do worker.

    O worker A abre o seu snapshot (como em ``gerar``); depois o worker B reserva
    outro job e cria o temporário. A limpeza de A não pode apagá-lo; só apaga o
    temporário de uma tentativa que já perdeu a posse.
    """
    from app.exportacoes.extracoes import ExportService, TIPO, armazenamento
    from app.jobs import JobService

    app = _mysql_isolado("US28_REVIEW_MYSQL_TEST_URL", tmp_path)
    cliente = app.test_client()
    for linha in ("1", "2"):
        assert cliente.post("/api/extracoes", json={**CORPO, "linha": linha}).status_code == 202
    with app.app_context():
        try:
            a = JobService().reservar(TIPO)
            PedidoExtracao.query.all()  # snapshot de A aberto antes da reserva de B
            loja = armazenamento()
            orfao = loja.diretorio / f".extracao-{a.id}-00000000-0000-0000-0000-000000000000.tmp"
            orfao.write_bytes(b"tentativa antiga")
            with app.app_context():  # worker B: outra sessão
                b = JobService().reservar(TIPO)
                vivo = loja.diretorio / f".extracao-{b.id}-{b.reserva_token}.tmp"
                vivo.write_bytes(b"lote em andamento")
                db.session.remove()

            ExportService()._limpar_temporarios(loja)

            assert vivo.exists(), "apagou o temporário de um job em posse de outro worker"
            assert not orfao.exists()
        finally:
            db.session.remove()
            db.engine.dispose()


def test_falha_real_entre_job_e_pedido_nao_deixa_orfaos(tmp_path, monkeypatch):
    """US28-T01 no MySQL: o job já foi inserido quando o INSERT do pedido viola
    uma FK de verdade (1452). A transação inteira é desfeita: nenhum órfão."""
    from app.exportacoes.extracoes import ExportService

    app = _mysql_isolado("US28_REVIEW_MYSQL_TEST_URL", tmp_path)

    def pedido_com_fk_invalida(pedido, job):
        assert db.session.get(ExecucaoProcessamento, job.id) is not None  # job já na transação
        db.session.add(PedidoExtracao(linha_codigo=pedido["linha"], inicio_periodo=pedido["inicio"],
                                      fim_periodo=pedido["fim"], formato=pedido["formato"],
                                      versao_base=job.versao_base, execucao_processamento_id="nao-existe"))
        db.session.flush()
    monkeypatch.setattr(ExportService, "_novo", staticmethod(pedido_com_fk_invalida))
    with app.app_context():
        try:
            resposta = app.test_client().post("/api/extracoes", json=CORPO)
            assert resposta.status_code == 503 and "nao-existe" not in resposta.get_data(as_text=True)
            db.session.remove()
            assert (PedidoExtracao.query.count(), ExecucaoProcessamento.query.count()) == (0, 0)
        finally:
            db.session.remove()
            db.engine.dispose()


INTERROMPER = """
import os, sys
from app import create_app
from app.exportacoes.extracoes import ExportService, executor_extracao
app = create_app({"SQLALCHEMY_DATABASE_URI": sys.argv[1], "EXPORTACOES_DIR": sys.argv[2], "MYSQL_SSL_CA": None})
servico = ExportService(tamanho_lote=2)
original = servico._progresso
def progresso(job_id, token, registros):
    original(job_id, token, registros)
    if registros >= 4:
        os._exit(9)  # morte abrupta no meio da gravação: sem finally, sem rollback
servico._progresso = progresso
executor_extracao(app, servico).executar_um()
"""


def test_fluxo_real_interrupcao_retomada_e_estados_no_mysql(tmp_path, monkeypatch):
    """US28 de ponta a ponta no MySQL 8: POST 202 → worker em outro processo morre
    gravando → reinício do Flask → retomada → pronto/download; falha de publicação
    e arquivo final ausente nunca viram 'pronto'; brutos intactos."""
    import subprocess
    import sys
    from datetime import datetime, timedelta, timezone
    from hashlib import sha256

    import pyarrow.parquet as pq

    from app.exportacoes.extracoes import ExportService, armazenamento, executor_extracao
    from app.jobs import JobService
    from app.models import ArquivoArmazenado, ExecucaoColeta, RegistroBrutoGPS
    from app.storage import FileStore

    app = _mysql_isolado("US28_REVIEW_MYSQL_TEST_URL", tmp_path)
    url = app.config["SQLALCHEMY_DATABASE_URI"].render_as_string(hide_password=False)
    inicio = datetime(2026, 11, 1, 3, tzinfo=timezone.utc)
    depois = lambda: datetime.now(timezone.utc) + timedelta(hours=1)  # reserva do morto vencida
    soma_brutos = text("SELECT COUNT(*), SUM(CRC32(CONCAT_WS('|', id, linha_origem, coletado_em, "
                       "CAST(payload AS CHAR)))) FROM registros_gps")
    with app.app_context():
        try:
            ciclo = ExecucaoColeta(iniciada_em=inicio)
            db.session.add(ciclo)
            db.session.flush()
            for minuto in range(9):
                db.session.add(RegistroBrutoGPS(execucao_id=ciclo.id, linha_origem="33887", payload={"m": minuto},
                                                coletado_em=inicio + timedelta(minutes=minuto)))
                db.session.add(RegistroBrutoGPS(execucao_id=ciclo.id, linha_origem="1111",
                                                coletado_em=inicio + timedelta(minutes=minuto)))
            db.session.add(RegistroBrutoGPS(execucao_id=ciclo.id, linha_origem="33887",
                                            coletado_em=inicio - timedelta(seconds=1)))  # fora do período
            db.session.commit()
            esperados = [i for (i,) in db.session.query(RegistroBrutoGPS.id).filter(
                RegistroBrutoGPS.linha_origem == "33887", RegistroBrutoGPS.coletado_em >= inicio)
                .order_by(RegistroBrutoGPS.coletado_em, RegistroBrutoGPS.id)]
            brutos_antes = db.session.execute(soma_brutos).one()
            db.session.remove()

            cliente = app.test_client()
            resposta = cliente.post("/api/extracoes", json={**CORPO, "formato": "PARQUET"})
            assert resposta.status_code == 202 and resposta.get_json()["estado"] == "aguardando"
            pedido_id = resposta.get_json()["id"]
            db.session.remove()

            # 1. Worker em processo separado morre no meio da gravação.
            ambiente = {**os.environ, "DATABASE_URL": url, "MYSQL_SSL_CA": "",
                        "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
            morto = subprocess.run([sys.executable, "-c", INTERROMPER, url, str(tmp_path)],
                                   cwd=Path(__file__).resolve().parents[1], env=ambiente,
                                   capture_output=True, text=True, timeout=120)
            assert morto.returncode == 9, morto.stderr
            loja = armazenamento().diretorio
            assert len(list(loja.glob(".extracao-*.tmp"))) == 1
            assert [p for p in loja.iterdir() if not p.name.startswith(".")] == []

            # 2. "Reinício do Flask": outra instância lê o mesmo estado persistido.
            reiniciado = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": make_url(url),
                                     "MYSQL_SSL_CA": None, "EXPORTACOES_DIR": str(tmp_path)})
            outro = reiniciado.test_client()
            corpo = outro.get(f"/api/extracoes/{pedido_id}").get_json()
            assert (corpo["estado"], corpo["arquivo"], corpo["progresso"]["registros"]) == ("processando", None, 4)
            assert outro.get(f"/api/extracoes/{pedido_id}/arquivo").status_code == 409

            # 3. Retomada após a reserva vencer: arquivo completo, único, sem temporário.
            assert executor_extracao(app, ExportService(tamanho_lote=2), relogio=depois).executar_um()[1] == "PRONTO"
            corpo = outro.get(f"/api/extracoes/{pedido_id}").get_json()
            assert corpo["estado"] == "pronto" and corpo["progresso"]["registros"] == len(esperados)
            baixado = outro.get(corpo["arquivo"]["download"])
            assert baixado.status_code == 200 and sha256(baixado.data).hexdigest() == corpo["arquivo"]["sha256"]
            arquivo = tmp_path / "baixado.parquet"
            arquivo.write_bytes(baixado.data)
            assert pq.read_table(arquivo).column("id").to_pylist() == esperados
            assert list(loja.glob(".extracao-*.tmp")) == []
            assert len([p for p in loja.iterdir() if not p.name.startswith(".")]) == 1
            assert ArquivoArmazenado.query.count() == 1
            assert executor_extracao(app, relogio=depois).executar_um() is None  # nada a refazer
            db.session.remove()

            # 4. Publicação falha: nunca 'pronto'; esgotadas as tentativas, 'erro' sem link.
            csv_id = cliente.post("/api/extracoes", json={**CORPO, "formato": "CSV"}).get_json()["id"]

            def falhar(*_):
                raise OSError("disco cheio")
            monkeypatch.setattr(FileStore, "publicar_extracao", falhar)
            for tentativa in range(1, JobService.LIMITE_TENTATIVAS + 1):
                assert executor_extracao(app, ExportService(tamanho_lote=2)).executar_um()[1] == "ERRO"
                corpo = outro.get(f"/api/extracoes/{csv_id}").get_json()
                esperado = "erro" if tentativa == JobService.LIMITE_TENTATIVAS else "aguardando"
                assert (corpo["estado"], corpo["arquivo"], corpo["progresso"]) == (esperado, None, None)
            assert corpo["erro"] == "OSError" and "disco cheio" not in str(corpo)
            assert outro.get(f"/api/extracoes/{csv_id}/arquivo").status_code == 409
            monkeypatch.undo()

            # 5. Job PRONTO cujo arquivo final sumiu: não é anunciado como pronto.
            (loja / Path(db.session.get(PedidoExtracao, pedido_id).arquivo.chave).name).unlink()
            corpo = outro.get(f"/api/extracoes/{pedido_id}").get_json()
            assert (corpo["estado"], corpo["erro"], corpo["arquivo"]) == ("erro", "ARQUIVO_INDISPONIVEL", None)
            assert outro.get(f"/api/extracoes/{pedido_id}/arquivo").status_code == 409

            assert db.session.execute(soma_brutos).one() == brutos_antes  # brutos não alterados
        finally:
            db.session.remove()
            db.engine.dispose()


def test_workers_concorrentes_executam_cada_extracao_uma_vez(tmp_path):
    """Reserva atômica no MySQL: 3 workers simultâneos, 4 pedidos; cada job tem uma
    única tentativa PRONTO, um arquivo final e nenhum temporário sobra (a limpeza
    de um worker não derruba a geração de outro)."""
    from datetime import datetime, timedelta, timezone

    from app.exportacoes.extracoes import ExportService, armazenamento, executor_extracao
    from app.models import ArquivoArmazenado, ExecucaoColeta, RegistroBrutoGPS

    app = _mysql_isolado("US28_REVIEW_MYSQL_TEST_URL", tmp_path)
    inicio = datetime(2026, 11, 1, 3, tzinfo=timezone.utc)
    linhas = ("11", "22", "33", "44")
    with app.app_context():
        try:
            ciclo = ExecucaoColeta(iniciada_em=inicio)
            db.session.add(ciclo)
            db.session.flush()
            db.session.add_all(RegistroBrutoGPS(execucao_id=ciclo.id, linha_origem=linha,
                                                coletado_em=inicio + timedelta(minutes=m))
                               for linha in linhas for m in range(30))
            db.session.commit()
            cliente = app.test_client()
            ids = [cliente.post("/api/extracoes", json={**CORPO, "linha": linha, "formato": "PARQUET"})
                   .get_json()["id"] for linha in linhas]
            db.session.remove()
            barreira = Barrier(3)

            def worker(_):
                barreira.wait()
                return executor_extracao(app, ExportService(tamanho_lote=4)).executar(uma_vez=True)

            with ThreadPoolExecutor(3) as executor:
                feitos = [job for lote in executor.map(worker, range(3)) for job in lote]

            assert sorted(e for _, e in feitos) == ["PRONTO"] * 4 and len({j for j, _ in feitos}) == 4
            for pedido_id in ids:
                pedido = db.session.get(PedidoExtracao, pedido_id)
                assert [(t["tentativa"], t["estado"], t["registros"]) for t in pedido.execucao.historico] == [
                    (1, "PRONTO", 30)]
            assert ArquivoArmazenado.query.count() == 4
            loja = armazenamento().diretorio
            assert list(loja.glob(".extracao-*.tmp")) == [] and len(list(loja.iterdir())) == 4
        finally:
            db.session.remove()
            db.engine.dispose()
