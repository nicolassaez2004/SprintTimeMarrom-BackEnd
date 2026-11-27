"""Opt-in (US20-T02B): exclusividade da reserva em MySQL 8 real.

Exige ``US20_T02B_MYSQL_TEST_URL`` apontando para uma base **vazia e descartável**
``us20_verificacao_*`` (nunca a base compartilhada). Aplica a cadeia Alembic e
usa conexões e processos independentes.
"""
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier

import pytest
from flask_migrate import upgrade
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url

from app import create_app, db
from app.jobs import JobService
from app.jobs.executor import Executor
from app.models import ExecucaoProcessamento
from app.reprocessamento.pedidos import TIPO, registrar_pedidos

RAIZ = Path(__file__).resolve().parents[1]
INICIO = datetime(2026, 11, 24, 12, tzinfo=timezone.utc)
THREADS = 8


def _pedir(n):
    inicio = INICIO + timedelta(minutes=15 * n)
    [p] = registrar_pedidos([{"indicador": "CUMPRIMENTO_VIAGENS", "inicio": inicio,
                              "fim": inicio + timedelta(minutes=15), "linha": "L", "sentido": "0"}],
                            versao_base="b1", versao_regra="cumprimento-1.1", versao_codigo="g1",
                            origem={"teste": n})
    return p.id


@pytest.fixture(scope="module")
def mysql_app():
    valor = os.getenv("US20_T02B_MYSQL_TEST_URL")
    if not valor:
        pytest.skip("Configure US20_T02B_MYSQL_TEST_URL para integração MySQL 8 isolada")
    url = make_url(valor)
    assert url.get_backend_name() == "mysql"
    assert url.database.startswith("us20_verificacao_"), "Recusa base de uso do time"
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": valor, "MYSQL_SSL_CA": None,
                      "SQLALCHEMY_ENGINE_OPTIONS": {"pool_size": THREADS + 2}})
    with app.app_context():
        assert not inspect(db.engine).get_table_names(), "Exige base vazia"
        versao = db.session.execute(text("SELECT VERSION()")).scalar_one()
        assert versao.startswith("8."), f"Exige MySQL 8, encontrado {versao}"
        upgrade(directory=str(RAIZ / "migrations"))
        db.session.remove()
    yield app, valor
    with app.app_context():
        db.engine.dispose()


def _novo_job(app, n):
    with app.app_context():
        try:
            return _pedir(n)
        finally:
            db.session.remove()


def test_reserva_simultanea_somente_um_assume(mysql_app):
    app, _ = mysql_app
    for rodada in range(20):
        job_id = _novo_job(app, rodada)
        barreira = Barrier(THREADS)

        def reservar(_):
            with app.app_context():
                try:
                    # Sem rollback: um rollback devolveria a conexão ao pool antes da
                    # barreira, e outra thread poderia reutilizá-la (falso "6 == 8").
                    conexao = db.session.execute(text("SELECT CONNECTION_ID()")).scalar_one()
                    barreira.wait()
                    job = JobService().reservar(TIPO)
                    return conexao, job.id if job else None
                finally:
                    db.session.remove()

        with ThreadPoolExecutor(THREADS) as pool:
            resultados = list(pool.map(reservar, range(THREADS)))
        assert len({c for c, _ in resultados}) == THREADS  # conexões independentes
        assert [j for _, j in resultados if j] == [job_id], f"rodada {rodada}"
        with app.app_context():
            job = db.session.get(ExecucaoProcessamento, job_id)
            assert (job.estado, job.tentativas) == ("PROCESSANDO", 1)
            job.estado, job.reserva_token, job.reserva_ate = "PRONTO", None, None
            db.session.commit()
            db.session.remove()


def test_dois_processos_worker_executam_cada_pedido_uma_vez(mysql_app):
    app, url = mysql_app
    ids = [_novo_job(app, 100 + n) for n in range(30)]
    env = {**os.environ, "DATABASE_URL": url, "MYSQL_SSL_CA": ""}
    processos = [subprocess.Popen([sys.executable, "-m", "app.jobs", "--uma-vez"], cwd=RAIZ, env=env)
                 for _ in range(2)]
    assert [p.wait(timeout=180) for p in processos] == [0, 0]
    with app.app_context():
        jobs = ExecucaoProcessamento.query.filter(ExecucaoProcessamento.id.in_(ids)).all()
        assert {(j.estado, j.tentativas) for j in jobs} == {("PRONTO", 1)}
        db.session.remove()


def test_executor_antigo_nao_publica_apos_perder_a_posse(mysql_app):
    app, _ = mysql_app
    job_id = _novo_job(app, 200)
    agora = datetime.now(timezone.utc)

    def outro_assume(job):
        # "Resultado" do executor A na sua sessão, ainda sem commit.
        db.session.add(ExecucaoProcessamento(
            tipo="RESULTADO_TESTE", chave_logica="resultado-a", periodo_inicio=INICIO,
            periodo_fim=INICIO + timedelta(minutes=15), bruto_id_maximo=0,
            versao_base="b1", versao_codigo="g1", versao_regra="r1"))
        db.session.flush()
        # Reserva de A já venceu: outro executor assume por outra conexão.
        with ThreadPoolExecutor(1) as pool:
            assert pool.submit(_reservar_em_outra_conexao, app).result() == job_id

    antigo = Executor(app, TIPO, outro_assume, duracao_reserva=timedelta(seconds=-1),
                      relogio=lambda: agora)
    assert antigo.executar_um() == (job_id, "ERRO")
    with app.app_context():
        job = db.session.get(ExecucaoProcessamento, job_id)
        assert (job.estado, job.tentativas, job.erro) == ("PROCESSANDO", 2, None)
        assert ExecucaoProcessamento.query.filter_by(chave_logica="resultado-a").count() == 0
        db.session.remove()


def _reservar_em_outra_conexao(app):
    with app.app_context():
        try:
            return JobService().reservar(TIPO).id
        finally:
            db.session.remove()
