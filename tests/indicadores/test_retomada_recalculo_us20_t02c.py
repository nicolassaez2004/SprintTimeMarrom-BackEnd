"""US20-T02C: retomada de recálculos interrompidos sem duplicar resultados (SQLite).

A base é um arquivo: cada ``app_context`` tem sessão e conexão próprias, então o
"outro worker" e o "novo processo" não compartilham transação com o primeiro.
A segurança transacional em MySQL 8 é comprovada por
``tests/test_retomada_recalculo_mysql.py`` (opt-in).
"""
from datetime import timedelta

import pytest
from sqlalchemy import event

from app import create_app, db
from app.jobs import JobService
from app.jobs.executor import Executor
from app.models import ExecucaoProcessamento, IndicadorOperacional, ViagemObservada
from app.reprocessamento.execucao import calcular_pedido, publicar_pedido
from app.reprocessamento.pedidos import TIPO, pedidos_pendentes, registrar_pedidos
from conftest import AGORA

RESERVA = JobService.RESERVA
DEPOIS = AGORA + RESERVA  # reserva tomada em AGORA já venceu
CONGELADOS = ("chave_logica", "versao_base", "versao_regra", "versao_codigo", "versao_gtfs_id",
              "bruto_id_maximo", "parametros", "periodo_inicio", "periodo_fim")


class Interrompido(BaseException):
    """Encerramento abrupto (SIGKILL/OOM): o executor não captura nem registra falha."""


@pytest.fixture
def config(tmp_path):
    return {"TESTING": True, "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'jobs.db'}",
            "MYSQL_SSL_CA": None}


@pytest.fixture
def app(config, tmp_path):
    aplicacao = create_app(config)
    aplicacao.instance_path = str(tmp_path / "instance")
    with aplicacao.app_context():
        event.listen(db.engine, "connect", lambda c, _: c.execute("PRAGMA foreign_keys=ON"))
        db.create_all()
        yield aplicacao
        db.session.remove()
        db.engine.dispose()


@pytest.fixture
def pedido(programacao):
    versao, _, _, partidas = programacao
    db.session.add(ViagemObservada(
        partida_programada=partidas[0], saida_observada=partidas[0].saida_prevista + timedelta(seconds=30),
        cobertura_tracado=0.9, versao_reconstrucao="recon-1", versao_correspondencia="match-1"))
    db.session.commit()
    return _pedir(gtfs=versao.id)


def _pedir(base="viagens_observadas", **escopo):
    item = {"indicador": "CUMPRIMENTO_VIAGENS", "inicio": AGORA, "fim": AGORA + timedelta(minutes=15),
            "linha": "3762-10", "sentido": "0", **escopo}
    [p] = registrar_pedidos([item], versao_base=base, versao_regra="cumprimento-1.1",
                            versao_codigo="git1", origem={"tipo": "TESTE"})
    return p.id


def _job(job_id):
    db.session.expire_all()
    return db.session.get(ExecucaoProcessamento, job_id)


def _executor(app, instante, calcular=calcular_pedido, publicar=publicar_pedido, **opcoes):
    return Executor(app, TIPO, calcular, publicar, relogio=lambda: instante, **opcoes)


def _historico(job_id):
    return [(t["tentativa"], t["estado"], t["erro"]) for t in _job(job_id).historico]


def _indicadores():
    db.session.expire_all()
    return IndicadorOperacional.query.filter_by(indicador="CUMPRIMENTO_VIAGENS").all()


def _em_outro_worker(app, funcao):
    with app.app_context():
        try:
            return funcao()
        finally:
            db.session.remove()


def test_interrupcao_reinicio_e_retomada_publicam_uma_unica_vez(app, config, pedido):
    """Teste obrigatório: reserva, encerramento no meio do cálculo, reinício e retomada."""
    congelado = {c: getattr(_job(pedido), c) for c in CONGELADOS}

    def morre_no_meio(job):
        calcular_pedido(job)
        assert _job(job.id).estado == "PROCESSANDO"  # reserva confirmada antes do cálculo
        raise Interrompido

    with pytest.raises(Interrompido):
        _executor(app, AGORA, morre_no_meio).executar_um()
    job = _job(pedido)
    assert (job.estado, job.tentativas, job.reserva_ate) == ("PROCESSANDO", 1, AGORA + RESERVA)
    assert _indicadores() == []
    db.session.remove()
    db.engine.dispose()  # fim do processo: só o banco guarda o estado

    reiniciado = create_app(config)
    assert _executor(reiniciado, AGORA + timedelta(minutes=14)).executar(uma_vez=True) == []
    assert _executor(reiniciado, DEPOIS).executar(uma_vez=True) == [(pedido, "PRONTO")]

    job = _job(pedido)
    assert (job.estado, job.tentativas, job.erro, job.reserva_token) == ("PRONTO", 2, None, None)
    assert _historico(pedido) == [(1, "INTERROMPIDA", "ReservaExpirada"), (2, "PRONTO", None)]
    assert {c: getattr(job, c) for c in CONGELADOS} == congelado
    [indicador] = _indicadores()
    publicado = indicador.publicado_em

    # Novo reinício após a publicação: nada é reservado nem republicado.
    assert _executor(create_app(config), DEPOIS + RESERVA).executar(uma_vez=True) == []
    assert [(i.id, i.publicado_em) for i in _indicadores()] == [(indicador.id, publicado)]
    assert _job(pedido).tentativas == 2


def test_reserva_valida_nao_e_assumida_e_vencida_e(app, pedido):
    primeiro = JobService().reservar(TIPO, AGORA)
    assert primeiro.id == pedido
    token = primeiro.reserva_token
    outro = JobService()
    assert outro.reservar(TIPO, DEPOIS - timedelta(seconds=1)) is None
    assert _job(pedido).reserva_token == token
    segundo = outro.reservar(TIPO, DEPOIS)
    assert segundo.id == pedido and segundo.reserva_token not in (None, token)
    assert (_job(pedido).estado, _job(pedido).tentativas) == ("PROCESSANDO", 2)


def test_worker_antigo_nao_publica_apos_perder_a_reserva(app, pedido):
    def outro_assume(job):
        resultados = calcular_pedido(job)
        assert _em_outro_worker(app, lambda: JobService().reservar(TIPO, DEPOIS).id) == pedido
        return resultados

    assert _executor(app, AGORA, outro_assume).executar_um() == (pedido, "ERRO")
    job = _job(pedido)
    # O ERRO é do worker antigo; o job continua com o novo dono, intacto.
    assert (job.estado, job.tentativas, job.erro) == ("PROCESSANDO", 2, None)
    assert _historico(pedido) == [(1, "INTERROMPIDA", "ReservaExpirada"), (2, "PROCESSANDO", None)]
    assert _indicadores() == []


def test_tentativas_sobrepostas_da_mesma_chave_publicam_uma_vez(app, pedido):
    def outra_tentativa_conclui(job):
        resultados = calcular_pedido(job)
        assert _executor(app, DEPOIS).executar_um() == (pedido, "PRONTO")
        return resultados

    assert _executor(app, AGORA, outra_tentativa_conclui).executar_um() == (pedido, "ERRO")
    assert (_job(pedido).estado, _job(pedido).tentativas) == ("PRONTO", 2)
    assert _historico(pedido) == [(1, "INTERROMPIDA", "ReservaExpirada"), (2, "PRONTO", None)]
    assert len(_indicadores()) == 1


def test_publicacao_repetida_nao_duplica_indicadores(app, pedido):
    job = _job(pedido)
    resultados = calcular_pedido(job)
    for _ in range(2):
        publicar_pedido(job, resultados)
        db.session.commit()
    # Outro pedido (outra versão de base) para a mesma janela: upsert, não duplicata.
    _pedir(base="viagens_observadas-v2", gtfs=job.versao_gtfs_id)
    assert {e for _, e in _executor(app, AGORA).executar(uma_vez=True)} == {"PRONTO"}
    assert len(_indicadores()) == 1


def test_falha_antes_da_publicacao_registra_e_retoma(app, pedido):
    def falha(job):
        raise TimeoutError("fonte lenta")

    assert _executor(app, AGORA, falha).executar_um() == (pedido, "ERRO")
    job = _job(pedido)
    assert (job.estado, job.tentativas, job.erro, job.reserva_token) == ("ERRO", 1, "TimeoutError", None)
    assert _indicadores() == []
    assert _executor(app, AGORA).executar_um() == (pedido, "PRONTO")
    assert _historico(pedido) == [(1, "ERRO", "TimeoutError"), (2, "PRONTO", None)]
    assert len(_indicadores()) == 1


def test_falha_durante_a_publicacao_faz_rollback(app, pedido):
    def publica_e_falha(job, resultados):
        assert publicar_pedido(job, resultados) == 1
        assert IndicadorOperacional.query.count() == 1  # gravado na transação, sem commit
        raise OSError("conexão caiu")

    assert _executor(app, AGORA, publicar=publica_e_falha).executar_um() == (pedido, "ERRO")
    assert _indicadores() == []
    assert (_job(pedido).estado, _job(pedido).erro) == ("ERRO", "OSError")
    assert _historico(pedido) == [(1, "ERRO", "OSError")]


def test_limite_de_tentativas_encerra_a_retomada(app, pedido):
    def sempre_falha(job):
        raise RuntimeError

    feitos = _executor(app, AGORA, sempre_falha).executar(uma_vez=True)
    limite = JobService.LIMITE_TENTATIVAS
    assert feitos == [(pedido, "ERRO")] * limite
    assert (_job(pedido).estado, _job(pedido).tentativas) == ("ERRO", limite)
    assert _historico(pedido) == [(n, "ERRO", "RuntimeError") for n in range(1, limite + 1)]
    assert _executor(app, AGORA + timedelta(days=1)).executar_um() is None
    assert pedidos_pendentes(AGORA) == []


def test_interrupcao_no_limite_vira_erro_sem_retomar(app, pedido):
    servico = JobService(limite=1)
    assert servico.reservar(TIPO, AGORA).tentativas == 1
    assert servico.reservar(TIPO, DEPOIS) is None
    job = _job(pedido)
    assert (job.estado, job.erro, job.reserva_token, job.reserva_ate) == ("ERRO", "ReservaExpirada", None, None)
    assert _historico(pedido) == [(1, "INTERROMPIDA", "ReservaExpirada")]


def test_espera_entre_tentativas_e_respeitada(app, pedido):
    servico = JobService(espera=timedelta(minutes=5))

    def falha(job):
        raise RuntimeError

    assert _executor(app, AGORA, falha, servico=servico).executar_um() == (pedido, "ERRO")
    assert _executor(app, AGORA + timedelta(minutes=4), servico=servico).executar_um() is None
    assert _executor(app, AGORA + timedelta(minutes=5), servico=servico).executar_um() == (pedido, "PRONTO")


def test_estados_persistidos_sao_coerentes(app, pedido):
    def falha(job):
        raise RuntimeError

    _executor(app, AGORA, falha).executar_um()
    JobService().reservar(TIPO, AGORA)  # tentativa 2 interrompida
    _executor(app, DEPOIS).executar_um()
    job = _job(pedido)
    historico = job.historico
    assert [t["tentativa"] for t in historico] == list(range(1, job.tentativas + 1)) == [1, 2, 3]
    assert all(t["inicio"] <= t["fim"] for t in historico)
    assert [t["estado"] for t in historico] == ["ERRO", "INTERROMPIDA", "PRONTO"]
    assert job.finalizada_em == DEPOIS and job.iniciada_em == DEPOIS
    assert (job.reserva_token, job.reserva_ate) == (None, None)
