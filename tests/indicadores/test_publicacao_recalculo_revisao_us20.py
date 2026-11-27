"""Revisão US20 (T01B/T02C): publicação do recálculo consistente e rastreável (SQLite).

- O registro publicado aponta o pedido, a chave, o snapshot e as versões usadas.
- Uma tentativa que leu dados antes não sobrescreve publicação posterior (geração
  mais nova do mesmo pedido ou publicação regular da agenda).
- Registros do mesmo indicador/regra/escopo/janela não reproduzidos saem na mesma
  transação; outras janelas, linhas e regras ficam intactas.
"""
from datetime import timedelta

from app import db
from app.jobs import JobService
from app.models import IndicadorOperacional
from app.reprocessamento.execucao import calcular_pedido, publicar_pedido
from app.reprocessamento.pedidos import TIPO, registrar_pedidos
from conftest import AGORA
from test_retomada_recalculo_us20_t02c import (  # noqa: F401 (fixtures)
    _executor, _indicadores, _job, app, config, pedido,
)

JANELA = timedelta(minutes=15)


def _registrar(gtfs, bruto_id_maximo):
    item = {"indicador": "CUMPRIMENTO_VIAGENS", "inicio": AGORA, "fim": AGORA + JANELA,
            "linha": "3762-10", "sentido": "0", "gtfs": gtfs}
    [p] = registrar_pedidos([item], versao_base="viagens_observadas", versao_regra="cumprimento-1.1",
                            versao_codigo="git1", origem={"tipo": "TESTE", "plano_id": "plano-x"},
                            bruto_id_maximo=bruto_id_maximo)
    return p.id


def _copia(registro, **mudancas):
    campos = {c.name: getattr(registro, c.name) for c in IndicadorOperacional.__table__.columns
              if c.name != "id"}
    return IndicadorOperacional(**{**campos, **mudancas})


def test_resultado_publicado_aponta_pedido_versoes_e_plano(app, pedido):
    assert _executor(app, AGORA).executar_um() == (pedido, "PRONTO")
    [registro] = _indicadores()
    job = _job(pedido)
    assert registro.qualidade["recalculo"] == {
        "pedido_id": job.id, "chave_logica": job.chave_logica,
        "snapshot_bruto_id_maximo": job.bruto_id_maximo, "versao_base": "viagens_observadas",
        "versao_regra": "cumprimento-1.1", "versao_codigo": "git1",
        "plano_id": None, "origem": "TESTE"}
    assert registro.versao_calculo == job.versao_regra


def test_geracao_antiga_nao_sobrescreve_a_mais_nova(app, programacao):
    versao, _, _, partidas = programacao
    from app.models import ViagemObservada
    db.session.add(ViagemObservada(partida_programada=partidas[0], cobertura_tracado=0.9,
        saida_observada=partidas[0].saida_prevista + timedelta(seconds=30),
        versao_reconstrucao="recon-1", versao_correspondencia="match-1"))
    db.session.commit()
    antigo = _registrar(versao.id, 1)
    job_antigo = JobService().reservar(TIPO, AGORA)  # leu o estado 1 e está calculando
    resultados_antigos = calcular_pedido(job_antigo)
    novo = _registrar(versao.id, 2)  # dado novo depois da leitura: nova geração
    assert novo != antigo and _job(novo).chave_logica.endswith(":2")
    assert _executor(app, AGORA + timedelta(minutes=1)).executar_um() == (novo, "PRONTO")
    job_antigo = _job(antigo)
    assert publicar_pedido(job_antigo, resultados_antigos) == 0  # preserva a geração nova
    db.session.commit()
    [registro] = _indicadores()
    assert registro.qualidade["recalculo"]["pedido_id"] == novo


def test_publicacao_regular_posterior_a_leitura_e_preservada(app, pedido):
    job = JobService().reservar(TIPO, AGORA)
    resultados = calcular_pedido(job)
    regular = _copia(IndicadorOperacional(**{k: v for k, v in resultados[0].items()}),
                     publicado_em=AGORA + timedelta(minutes=5), valor=12.5)
    db.session.add(regular)  # agenda publicou a mesma chave depois de o worker ler
    db.session.commit()
    job = _job(pedido)
    assert publicar_pedido(job, resultados) == 0
    db.session.commit()
    assert [r.valor for r in _indicadores()] == [12.5]


def test_substitui_somente_o_mesmo_escopo_janela_e_regra(app, pedido):
    job = _job(pedido)
    resultados = calcular_pedido(job)
    base = IndicadorOperacional(**resultados[0])
    db.session.add_all([
        _copia(base, chave_resultado="captura-antiga", publicado_em=AGORA - timedelta(hours=1)),
        _copia(base, chave_resultado="outra-janela", periodo_inicio=AGORA + JANELA,
               periodo_fim=AGORA + 2 * JANELA, publicado_em=AGORA - timedelta(hours=1)),
        _copia(base, chave_resultado="outra-regra", versao_calculo="cumprimento-1.0",
               publicado_em=AGORA - timedelta(hours=1)),
        _copia(base, chave_resultado="outro-sentido", sentido="1",
               publicado_em=AGORA - timedelta(hours=1)),
    ])
    db.session.commit()
    assert _executor(app, AGORA).executar_um() == (pedido, "PRONTO")
    chaves = {r.chave_resultado for r in _indicadores()}
    assert chaves == {resultados[0]["chave_resultado"], "outra-janela", "outra-regra", "outro-sentido"}


def test_worker_continuo_sobrevive_a_banco_indisponivel(app, pedido):
    """T02B: falha de infraestrutura fora do job não derruba o laço contínuo."""
    from threading import Event

    from sqlalchemy.exc import OperationalError

    servico, chamadas = JobService(), []
    original = servico.reservar

    def reservar(*args, **kwargs):
        chamadas.append(1)
        if len(chamadas) == 1:
            raise OperationalError("SELECT", {}, Exception("banco fora"))
        return original(*args, **kwargs)

    servico.reservar = reservar
    parar = Event()
    executor = _executor(app, AGORA, servico=servico)
    executar_um = executor.executar_um

    def uma_rodada():
        resultado = executar_um()
        if resultado is not None:
            parar.set()
        return resultado

    executor.executar_um = uma_rodada
    assert executor.executar(parar, intervalo=0) == [(pedido, "PRONTO")]
    assert len(chamadas) == 2
