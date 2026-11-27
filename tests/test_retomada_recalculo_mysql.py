"""Opt-in (US20-T02C): retomada de recálculos em MySQL 8 real.

Exige ``US20_T02C_MYSQL_TEST_URL`` apontando para uma base **vazia e descartável**
``us20_verificacao_*`` (nunca a base compartilhada). Aplica a cadeia Alembic e
usa processos e conexões independentes. O usuário precisa de
``SELECT ON performance_schema.data_lock_waits`` para observar a espera de bloqueio.
"""
import os
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier

import pytest
from flask_migrate import upgrade
from sqlalchemy import inspect, text, update
from sqlalchemy.engine import make_url

from app import create_app, db
from app.jobs import JobService
from app.jobs.executor import Executor
from app.models import (ArquivoArmazenado, ExecucaoProcessamento, ImportacaoProgramacao,
                        IndicadorOperacional, Linha, PartidaProgramada, VersaoGTFS,
                        ViagemObservada)
from app.reprocessamento.execucao import calcular_pedido, publicar_pedido
from app.reprocessamento.pedidos import TIPO, registrar_pedidos

RAIZ = Path(__file__).resolve().parents[1]
INICIO = datetime(2026, 11, 24, 12, tzinfo=timezone.utc)
THREADS = 8
FILHO = """
import sys, time
from datetime import timedelta
from app import create_app
from app.jobs.executor import Executor
from app.reprocessamento.execucao import calcular_pedido, publicar_pedido
from app.reprocessamento.pedidos import TIPO

def calcular(job):
    resultados = calcular_pedido(job)
    print("RESERVADO", job.id, job.reserva_token, flush=True)
    time.sleep(300)  # morto aqui por SIGKILL
    return resultados

Executor(create_app(), TIPO, calcular, publicar_pedido,
         duracao_reserva=timedelta(seconds=2)).executar_um()
"""


@pytest.fixture(scope="module")
def mysql_app():
    valor = os.getenv("US20_T02C_MYSQL_TEST_URL")
    if not valor:
        pytest.skip("Configure US20_T02C_MYSQL_TEST_URL para integração MySQL 8 isolada")
    url = make_url(valor)
    assert url.get_backend_name() == "mysql"
    assert url.database.startswith("us20_verificacao_"), "Recusa base de uso do time"
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": valor, "MYSQL_SSL_CA": None,
                      "SQLALCHEMY_ENGINE_OPTIONS": {"pool_size": THREADS + 4}})
    with app.app_context():
        assert not inspect(db.engine).get_table_names(), "Exige base vazia"
        versao = db.session.execute(text("SELECT VERSION()")).scalar_one()
        assert versao.startswith("8."), f"Exige MySQL 8, encontrado {versao}"
        upgrade(directory=str(RAIZ / "migrations"))
        gtfs = _semear()
        db.session.remove()
    yield app, valor, gtfs
    with app.app_context():
        db.engine.dispose()


def _semear():
    """Uma linha com 3 partidas e uma viagem observada: o cumprimento publica 1 indicador."""
    arquivo = ArquivoArmazenado(chave="gtfs/t02c.zip", nome_original="t02c.zip",
                                mime_type="application/zip", tamanho_bytes=0, sha256="c" * 64,
                                criado_em=INICIO)
    versao = VersaoGTFS(hash="c" * 64, capturada_em=INICIO, vigencia_inicio=date(2026, 1, 1),
                        vigencia_fim=date(2026, 12, 31), arquivo=arquivo)
    linha = Linha(versao_gtfs=versao, codigo="3762-10", nome="Linha sintética")
    db.session.add_all([versao, linha])
    db.session.flush()
    importacao = ImportacaoProgramacao(
        versao_gtfs_id=versao.id, linha=linha, data_operacao=date(2026, 11, 24), sentido="0",
        tipo_dia="UTIL", fonte="OFICIAL", versao_fonte="captura-1",
        fonte_url="https://example.invalid/grade", capturada_em=INICIO, verificada_em=INICIO,
        arquivo=arquivo)
    partidas = [PartidaProgramada(importacao=importacao, ordem=n, tipo_oferta="FIXA",
                                  saida_prevista=INICIO + timedelta(minutes=15 * (n - 1)))
                for n in (1, 2, 3)]
    db.session.add_all([importacao, *partidas, ViagemObservada(
        partida_programada=partidas[0], saida_observada=INICIO + timedelta(seconds=30),
        cobertura_tracado=0.9, versao_reconstrucao="recon-1", versao_correspondencia="match-1")])
    db.session.commit()
    return versao.id


def _em_contexto(app, funcao):
    with app.app_context():
        try:
            return funcao()
        finally:
            db.session.remove()


def _pedir(app, gtfs, base):
    item = {"indicador": "CUMPRIMENTO_VIAGENS", "inicio": INICIO,
            "fim": INICIO + timedelta(minutes=15), "linha": "3762-10", "sentido": "0", "gtfs": gtfs}
    return _em_contexto(app, lambda: registrar_pedidos(
        [item], versao_base=base, versao_regra="cumprimento-1.1", versao_codigo="g1",
        origem={"teste": base})[0].id)


def _ler(app, job_id):
    def ler():
        job = db.session.get(ExecucaoProcessamento, job_id)
        historico = [(t["tentativa"], t["estado"], t["erro"]) for t in job.historico or []]
        indicadores = [(i.id, i.publicado_em) for i in IndicadorOperacional.query]
        return job.estado, job.tentativas, historico, indicadores
    return _em_contexto(app, ler)


def _limpar_indicadores(app):
    def limpar():
        IndicadorOperacional.query.delete()
        db.session.commit()
    _em_contexto(app, limpar)


def test_processo_morto_no_calculo_e_retomado_por_novo_processo(mysql_app):
    """Teste obrigatório: SIGKILL no meio do cálculo, reinício e retomada uma única vez."""
    app, url, gtfs = mysql_app
    _limpar_indicadores(app)
    job_id = _pedir(app, gtfs, "interrupcao")
    env = {**os.environ, "DATABASE_URL": url, "MYSQL_SSL_CA": ""}
    filho = subprocess.Popen([sys.executable, "-c", FILHO], cwd=RAIZ, env=env,
                             stdout=subprocess.PIPE, text=True)
    try:
        linha = filho.stdout.readline().split()
        assert linha[:2] == ["RESERVADO", job_id]
        assert _ler(app, job_id)[:3] == ("PROCESSANDO", 1, [(1, "PROCESSANDO", None)])
    finally:
        filho.send_signal(signal.SIGKILL)
        filho.wait(timeout=30)
    assert filho.returncode == -signal.SIGKILL
    assert _ler(app, job_id)[3] == []  # nada publicado pelo processo morto

    time.sleep(2.5)  # condição de retomada: reserva de 2 s vencida
    worker = [sys.executable, "-m", "app.jobs", "--uma-vez"]
    subprocess.run(worker, cwd=RAIZ, env=env, check=True, timeout=120)
    estado, tentativas, historico, indicadores = _ler(app, job_id)
    assert (estado, tentativas) == ("PRONTO", 2)
    assert historico == [(1, "INTERROMPIDA", "ReservaExpirada"), (2, "PRONTO", None)]
    assert len(indicadores) == 1

    subprocess.run(worker, cwd=RAIZ, env=env, check=True, timeout=120)  # novo reinício
    assert _ler(app, job_id) == (estado, tentativas, historico, indicadores)


def test_oito_conexoes_recuperam_e_reservam_o_mesmo_job_uma_vez(mysql_app):
    app, _, gtfs = mysql_app
    for rodada in range(10):
        job_id = _pedir(app, gtfs, f"disputa-{rodada}")
        assert _em_contexto(app, lambda: JobService().reservar(TIPO, INICIO).id) == job_id
        barreira = Barrier(THREADS)

        def disputar(_):
            def reservar():
                # Sem rollback: cada thread retém a sua conexão até reservar. CONNECTION_ID()
                # não lê tabelas InnoDB, então não fixa o snapshot da transação.
                conexao = db.session.execute(text("SELECT CONNECTION_ID()")).scalar_one()
                barreira.wait()
                job = JobService().reservar(TIPO, INICIO + timedelta(hours=1))
                return conexao, job.id if job else None
            return _em_contexto(app, reservar)

        with ThreadPoolExecutor(THREADS) as pool:
            resultados = list(pool.map(disputar, range(THREADS)))
        assert len({c for c, _ in resultados}) == THREADS
        assert [j for _, j in resultados if j] == [job_id], f"rodada {rodada}"
        estado, tentativas, historico, _ = _ler(app, job_id)
        assert (estado, tentativas) == ("PROCESSANDO", 2)
        assert historico == [(1, "INTERROMPIDA", "ReservaExpirada"), (2, "PROCESSANDO", None)]

        def encerrar():
            db.session.execute(update(ExecucaoProcessamento).where(ExecucaoProcessamento.id == job_id)
                               .values(estado="PRONTO", reserva_token=None, reserva_ate=None))
            db.session.commit()
        _em_contexto(app, encerrar)


def test_worker_antigo_perde_a_posse_e_so_o_novo_publica(mysql_app):
    app, _, gtfs = mysql_app
    _limpar_indicadores(app)
    job_id = _pedir(app, gtfs, "sobreposicao")
    publicacoes = []

    def publicar(job, resultados):
        publicacoes.append(job.reserva_token)
        return publicar_pedido(job, resultados)

    def novo_worker_conclui(job):
        resultados = calcular_pedido(job)
        with ThreadPoolExecutor(1) as pool:  # outra thread = outra conexão
            assert pool.submit(Executor(app, TIPO, calcular_pedido, publicar,
                                        relogio=lambda: INICIO + timedelta(hours=1)).executar_um
                               ).result() == (job_id, "PRONTO")
        return resultados

    antigo = Executor(app, TIPO, novo_worker_conclui, publicar, relogio=lambda: INICIO)
    assert antigo.executar_um() == (job_id, "ERRO")  # PossePerdida, sem gravar
    estado, tentativas, historico, indicadores = _ler(app, job_id)
    assert (estado, tentativas) == ("PRONTO", 2)
    assert historico == [(1, "INTERROMPIDA", "ReservaExpirada"), (2, "PRONTO", None)]
    assert len(publicacoes) == 1 and len(indicadores) == 1


def test_publicacao_bloqueia_recuperacao_concorrente(mysql_app):
    """Com a posse validada por FOR UPDATE, quem tenta recuperar espera e encontra PRONTO."""
    app, _, gtfs = mysql_app
    job_id = _pedir(app, gtfs, "bloqueio")
    pool, futuro = ThreadPoolExecutor(1), {}

    def publicar_com_concorrente(job, resultados):
        # Job travado por este executor; outro (outra conexão) tenta recuperá-lo como vencido.
        futuro["outro"] = pool.submit(_em_contexto, app, lambda: JobService().reservar(
            TIPO, INICIO + timedelta(hours=1)))
        assert _esperar_bloqueio(app), "o outro executor deveria aguardar o bloqueio do job"
        return publicar_pedido(job, resultados)

    executor = Executor(app, TIPO, calcular_pedido, publicar_com_concorrente, relogio=lambda: INICIO)
    try:
        assert executor.executar_um() == (job_id, "PRONTO")
        assert futuro["outro"].result(timeout=60) is None  # reavaliou o WHERE: já PRONTO
    finally:
        pool.shutdown()
    estado, tentativas, historico, _ = _ler(app, job_id)
    assert (estado, tentativas, historico) == ("PRONTO", 1, [(1, "PRONTO", None)])


def _esperar_bloqueio(app, limite=10.0):
    fim = time.monotonic() + limite
    while time.monotonic() < fim:
        esperando = _em_contexto(app, lambda: db.session.execute(text(
            "SELECT COUNT(*) FROM performance_schema.data_lock_waits")).scalar_one())
        if esperando:
            return True
        time.sleep(0.05)
    return False


def test_dois_processos_retomam_jobs_interrompidos_uma_vez(mysql_app):
    app, url, gtfs = mysql_app
    ids = [_pedir(app, gtfs, f"lote-{n}") for n in range(20)]

    def interromper():  # processo morto: PROCESSANDO com reserva vencida
        db.session.execute(update(ExecucaoProcessamento).where(ExecucaoProcessamento.id.in_(ids))
                           .values(estado="PROCESSANDO", tentativas=1, reserva_token="morto",
                                   reserva_ate=INICIO, iniciada_em=INICIO - timedelta(minutes=15),
                                   historico=[{"tentativa": 1, "inicio": INICIO.isoformat(),
                                               "fim": None, "estado": "PROCESSANDO", "erro": None}]))
        db.session.commit()
    _em_contexto(app, interromper)
    env = {**os.environ, "DATABASE_URL": url, "MYSQL_SSL_CA": ""}
    processos = [subprocess.Popen([sys.executable, "-m", "app.jobs", "--uma-vez"], cwd=RAIZ, env=env)
                 for _ in range(2)]
    assert [p.wait(timeout=300) for p in processos] == [0, 0]
    for job_id in ids:
        estado, tentativas, historico, _ = _ler(app, job_id)
        assert (estado, tentativas) == ("PRONTO", 2)
        assert historico == [(1, "INTERROMPIDA", "ReservaExpirada"), (2, "PRONTO", None)]
    assert len(_ler(app, ids[0])[3]) == 1  # mesma janela: um único indicador
