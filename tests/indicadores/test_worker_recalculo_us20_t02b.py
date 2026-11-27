"""US20-T02B: reserva e execução de pedidos de recálculo fora das rotas Flask (SQLite).

A exclusividade transacional real é comprovada em MySQL 8 por
``tests/test_worker_recalculo_mysql.py`` (opt-in).
"""
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import update

from app import create_app, db
from app.jobs import JobService
from app.jobs.executor import Executor
from app.models import ExecucaoProcessamento, IndicadorOperacional, ViagemObservada
from app.reprocessamento.execucao import recalcular_pedido
from app.reprocessamento.pedidos import TIPO, registrar_pedidos
from conftest import AGORA

JANELA = timedelta(minutes=15)
REGRA = "cumprimento-1.1"
RAIZ = Path(__file__).resolve().parents[2]


def _pedir(n=0, regra=REGRA, **escopo):
    inicio = AGORA + n * JANELA
    item = {"indicador": "CUMPRIMENTO_VIAGENS", "inicio": inicio, "fim": inicio + JANELA,
            "linha": "3762-10", "sentido": "0", **escopo}
    [pedido] = registrar_pedidos([item], versao_base="viagens_observadas", versao_regra=regra,
                                 versao_codigo="git1", origem={"tipo": "TESTE"})
    return pedido.id


def _job(job_id):
    db.session.expire_all()
    return db.session.get(ExecucaoProcessamento, job_id)


def _registrar_tarefa(chamadas, efeito=None):
    def tarefa(job):
        chamadas.append((job.id, db.session()))
        if efeito:
            efeito(job)
    return tarefa


@pytest.fixture
def viagens(programacao):
    _, _, _, partidas = programacao
    db.session.add(ViagemObservada(
        partida_programada=partidas[0], saida_observada=partidas[0].saida_prevista + timedelta(seconds=30),
        cobertura_tracado=0.9, versao_reconstrucao="recon-1", versao_correspondencia="match-1"))
    db.session.commit()
    return programacao


def test_executor_reserva_e_executa_o_recalculo_existente(app, viagens):
    versao = viagens[0]
    job_id = _pedir(gtfs=versao.id)
    assert Executor(app, TIPO, recalcular_pedido).executar_um() == (job_id, "PRONTO")
    job = _job(job_id)
    assert (job.estado, job.tentativas, job.erro) == ("PRONTO", 1, None)
    assert job.iniciada_em is not None and job.finalizada_em >= job.iniciada_em
    assert job.reserva_token is None and job.reserva_ate is None
    [indicador] = IndicadorOperacional.query.filter_by(indicador="CUMPRIMENTO_VIAGENS").all()
    assert (indicador.periodo_inicio, indicador.valor, indicador.versao_calculo) == (AGORA, 100.0, REGRA)
    assert Executor(app, TIPO, recalcular_pedido).executar_um() is None  # nada mais a fazer


def test_preserva_chave_versoes_escopo_e_parametros(app, programacao):
    job_id = _pedir(gtfs=programacao[0].id)
    antes = _job(job_id)
    congelado = (antes.chave_logica, antes.versao_base, antes.versao_regra, antes.versao_codigo,
                 antes.versao_gtfs_id, antes.bruto_id_maximo, dict(antes.parametros),
                 antes.periodo_inicio, antes.periodo_fim)
    Executor(app, TIPO, recalcular_pedido).executar_um()
    depois = _job(job_id)
    assert (depois.chave_logica, depois.versao_base, depois.versao_regra, depois.versao_codigo,
            depois.versao_gtfs_id, depois.bruto_id_maximo, depois.parametros,
            depois.periodo_inicio, depois.periodo_fim) == congelado


def test_dois_pedidos_diferentes_sao_executados_separadamente(app, programacao):
    ids = {_pedir(0), _pedir(1)}
    chamadas = []
    feitos = Executor(app, TIPO, _registrar_tarefa(chamadas)).executar(uma_vez=True)
    assert {i for i, _ in feitos} == ids and {e for _, e in feitos} == {"PRONTO"}
    assert sorted(i for i, _ in chamadas) == sorted(ids)
    assert all(_job(i).tentativas == 1 for i in ids)


def test_reserva_concorrente_somente_um_assume(app, programacao):
    job_id = _pedir()
    servico = JobService()
    primeiro = servico.reservar(TIPO, AGORA)
    assert primeiro.id == job_id and primeiro.reserva_token
    assert servico.reservar(TIPO, AGORA + timedelta(minutes=1)) is None
    job = _job(job_id)
    assert (job.estado, job.tentativas) == ("PROCESSANDO", 1)


def test_sessoes_independentes_e_fechadas_por_execucao(app, programacao):
    _pedir(0), _pedir(1)
    chamadas = []
    Executor(app, TIPO, _registrar_tarefa(chamadas)).executar(uma_vez=True)
    (_, s1), (_, s2) = chamadas
    assert s1 is not s2 and s1 is not db.session()
    for sessao in (s1, s2):
        assert not sessao.in_transaction() and len(sessao.identity_map) == 0


def test_falha_faz_rollback_registra_erro_e_fecha_sessao(app, viagens):
    job_id = _pedir(gtfs=viagens[0].id)
    chamadas = []

    def grava_e_falha(job):
        recalcular_pedido(job)  # resultados na sessão, sem commit
        assert IndicadorOperacional.query.count() == 1
        raise RuntimeError("falha no meio do cálculo")

    assert Executor(app, TIPO, _registrar_tarefa(chamadas, grava_e_falha)).executar_um() == (
        job_id, "ERRO")
    job = _job(job_id)
    assert (job.estado, job.tentativas, job.erro) == ("ERRO", 1, "RuntimeError")
    assert job.reserva_token is None and job.finalizada_em is not None
    assert IndicadorOperacional.query.count() == 0  # nada parcial publicado
    sessao = chamadas[0][1]
    assert not sessao.in_transaction() and len(sessao.identity_map) == 0


def test_regra_nao_implementada_vira_erro_sem_publicar(app, viagens):
    job_id = _pedir(regra="cumprimento-9.9", gtfs=viagens[0].id)
    assert Executor(app, TIPO, recalcular_pedido).executar_um() == (job_id, "ERRO")
    assert _job(job_id).erro == "RegraIndisponivel"
    assert IndicadorOperacional.query.count() == 0


@pytest.mark.parametrize("estado,tentativas,reserva", [
    ("PRONTO", 1, None), ("ERRO", JobService.LIMITE_TENTATIVAS, None),  # ERRO esgotado (T02C)
    ("PROCESSANDO", 1, timedelta(minutes=10))])
def test_pedido_indisponivel_nao_e_executado(app, programacao, estado, tentativas, reserva):
    job_id = _pedir()
    job = _job(job_id)
    job.estado, job.tentativas = estado, tentativas
    if reserva:
        job.reserva_token, job.reserva_ate = "outro", AGORA + reserva
    db.session.commit()
    chamadas = []
    executor = Executor(app, TIPO, _registrar_tarefa(chamadas), relogio=lambda: AGORA)
    assert executor.executar_um() is None and chamadas == []
    assert (_job(job_id).estado, _job(job_id).tentativas) == (estado, tentativas)


def test_posse_perdida_descarta_resultados(app, viagens):
    job_id = _pedir(gtfs=viagens[0].id)

    def outro_executor_assume(job):
        recalcular_pedido(job)
        # Simula a reserva vencida e assumida por outro executor (outra conexão no MySQL).
        db.session.execute(update(ExecucaoProcessamento).where(ExecucaoProcessamento.id == job.id)
                           .values(reserva_token="outro", tentativas=2))

    assert Executor(app, TIPO, outro_executor_assume).executar_um() == (job_id, "ERRO")
    job = _job(job_id)
    # O rollback desfez também a simulação; o essencial é que nada foi publicado
    # e o executor antigo não marcou PRONTO.
    assert (job.estado, job.erro) == ("ERRO", "PossePerdida")
    assert IndicadorOperacional.query.count() == 0


def test_reinicio_preserva_estado_e_retoma_reserva_vencida(tmp_path):
    uri = f"sqlite:///{tmp_path / 'jobs.db'}"
    config = {"TESTING": True, "SQLALCHEMY_DATABASE_URI": uri, "MYSQL_SSL_CA": None}
    primeiro = create_app(config)
    with primeiro.app_context():
        db.create_all()
        pronto, pendente, preso = _pedir(0), _pedir(1), _pedir(2)
        db.session.execute(update(ExecucaoProcessamento).where(ExecucaoProcessamento.id == pronto)
                           .values(estado="PRONTO", tentativas=1))
        # Processo morto no meio: PROCESSANDO com reserva já vencida.
        db.session.execute(update(ExecucaoProcessamento).where(ExecucaoProcessamento.id == preso)
                           .values(estado="PROCESSANDO", tentativas=1, reserva_token="morto",
                                   reserva_ate=AGORA - timedelta(minutes=1)))
        db.session.commit()
        db.session.remove()
        db.engine.dispose()

    segundo = create_app(config)  # novo processo: só o banco carrega o estado
    chamadas = []
    feitos = Executor(segundo, TIPO, _registrar_tarefa(chamadas), relogio=lambda: AGORA).executar(
        uma_vez=True)
    assert sorted(i for i, _ in feitos) == sorted([pendente, preso])
    with segundo.app_context():
        estados = {j.id: (j.estado, j.tentativas) for j in ExecucaoProcessamento.query}
        db.engine.dispose()
    assert estados == {pronto: ("PRONTO", 1), pendente: ("PRONTO", 1), preso: ("PRONTO", 2)}


def test_executor_fica_fora_das_rotas_http():
    """O processo web não importa o worker; nenhuma rota pertence a ele."""
    codigo = ("import sys; from app import create_app;"
              "app = create_app({'TESTING': True, 'SQLALCHEMY_DATABASE_URI': 'sqlite://'});"
              "mods = {v.__module__ for v in app.view_functions.values()};"
              "assert 'app.jobs.executor' not in sys.modules, 'worker importado pelo web';"
              "assert not any(m.startswith(('app.jobs', 'app.reprocessamento')) for m in mods), mods")
    subprocess.run([sys.executable, "-c", codigo], cwd=RAIZ, check=True)


def test_solicitar_responde_sem_aguardar_o_recalculo(app, viagens):
    """O que uma rota pode fazer (registrar) devolve AGUARDANDO sem calcular nada."""
    job = _job(_pedir(gtfs=viagens[0].id))
    assert (job.estado, job.tentativas, job.iniciada_em) == ("AGUARDANDO", 0, None)
    assert IndicadorOperacional.query.count() == 0


def test_cli_uma_vez_executa_fora_do_flask(tmp_path):
    uri = f"sqlite:///{tmp_path / 'cli.db'}"
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": uri, "MYSQL_SSL_CA": None})
    with app.app_context():
        db.create_all()
        job_id = _pedir()
        db.session.remove()
        db.engine.dispose()
    env = {**__import__("os").environ, "DATABASE_URL": uri, "MYSQL_SSL_CA": ""}
    subprocess.run([sys.executable, "-m", "app.jobs", "--uma-vez"], cwd=RAIZ, env=env,
                   check=True, timeout=60)
    with app.app_context():
        job = db.session.get(ExecucaoProcessamento, job_id)
        assert (job.estado, job.tentativas) == ("PRONTO", 1)  # sem viagens: 0 registros
        db.engine.dispose()
