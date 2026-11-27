"""US20-T04: dado tardio, mudança de regra, reinício e falha (dados SINTÉTICOS, SQLite em arquivo).

Linha 3762-10, sentido 0, data operacional 24/11/2026; partidas FIXAS 12:00, 12:15 e
12:30 UTC (09:00, 09:15 e 09:30 em São Paulo). A viagem da partida de 12:00 é
observada (saída 12:00:30, cobertura 0,9).

Referência MANUAL: outra base, já com os dados finais, consolidada à mão pela
agenda (``consolidar_dia``), sem worker nem plano. O recálculo automático precisa
chegar exatamente aos mesmos registros (indicador, janela, regra, valor e contagens).
Conta à mão do caso tardio (CUMPRIMENTO_VIAGENS, janela 12:15–12:30):
antes = 0 realizadas ÷ 1 programada = 0%; depois da viagem tardia = 1 ÷ 1 = 100%.

- Dado tardio: posição de 12:20 recebida às 09:00 UTC de 25/11, depois da
  consolidação (08:00 UTC); a reconstrução forma a viagem da partida de 12:15.
  Gatilho: ``planejar_tardio`` → ``registrar_pedidos_do_plano`` → worker.
- Mudança de regra: o conjunto foi publicado por uma regra antiga
  (``cumprimento-0.9``, que contava a viagem de 12:15 com cobertura 0,5). Gatilho:
  ``planejar_regra`` para a regra vigente → worker.
"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app import create_app, db
from app.exportacoes.parquet import periodo_utc
from app.indicadores.agenda import consolidar_dia, versao_vigente
from app.indicadores.cumprimento import VERSAO_ALGORITMO
from app.jobs import JobService
from app.models import (
    ArquivoArmazenado, ExecucaoColeta, ExecucaoProcessamento, ImportacaoProgramacao, IndicadorOperacional,
    Linha, ObservacaoTratada, PartidaProgramada, RegistroBrutoGPS, VersaoGTFS, ViagemObservada,
)
from app.reprocessamento import execucao as recalculo
from app.reprocessamento.pedidos import TIPO, registrar_pedidos_do_plano
from app.reprocessamento.planos import planejar_regra, planejar_tardio
from test_retomada_recalculo_us20_t02c import Interrompido, app, config  # noqa: F401 (fixtures)

UTC = timezone.utc
T0 = datetime(2026, 11, 24, 12, tzinfo=UTC)
DIA = date(2026, 11, 24)
CONSOLIDADO_EM = datetime(2026, 11, 25, 8, tzinfo=UTC)   # 05:00 em São Paulo
RECEBIDO_EM = datetime(2026, 11, 25, 9, tzinfo=UTC)       # chegada da posição tardia
WORKER = datetime(2026, 11, 25, 9, 30, tzinfo=UTC)
LACUNA = timedelta(seconds=120)
VERSOES = dict(versao_base="viagens_observadas", versao_codigo="git-us20-t04")
REGRA_ANTIGA = "cumprimento-0.9"
M = lambda n: timedelta(minutes=n)  # noqa: E731


def _semear(tardia, cobertura_tardia=0.9):
    """Programação e viagens. ``tardia``: a viagem da partida de 12:15 já existe."""
    arquivo = ArquivoArmazenado(chave="gtfs/t04.zip", nome_original="t04.zip", mime_type="application/zip",
                                tamanho_bytes=0, sha256="b" * 64, criado_em=T0)
    versao = VersaoGTFS(hash="b" * 64, capturada_em=T0, vigencia_inicio=date(2026, 1, 1),
                        vigencia_fim=date(2026, 12, 31), arquivo=arquivo)
    linha = Linha(versao_gtfs=versao, codigo="3762-10", nome="Linha sintética")
    db.session.add_all([versao, linha])
    db.session.flush()
    importacao = ImportacaoProgramacao(
        versao_gtfs_id=versao.id, linha=linha, data_operacao=DIA, sentido="0", tipo_dia="UTIL",
        fonte="OFICIAL", versao_fonte="captura-1", fonte_url="https://example.invalid/grade",
        capturada_em=T0, verificada_em=T0, arquivo=arquivo)
    partidas = [PartidaProgramada(importacao=importacao, ordem=i, saida_prevista=T0 + M(m), tipo_oferta="FIXA")
                for i, m in enumerate((0, 15, 30), start=1)]
    db.session.add_all([importacao, *partidas])
    db.session.add(_viagem(partidas[0], T0 + timedelta(seconds=30), T0 + M(14)))
    if tardia:
        db.session.add(_viagem(partidas[1], T0 + M(16), T0 + M(29), cobertura_tardia))
    db.session.commit()
    return versao, partidas


def _viagem(partida, saida, fim, cobertura=0.9):
    return ViagemObservada(partida_programada=partida, saida_observada=saida, fim_observado=fim,
                           cobertura_tracado=cobertura, versao_reconstrucao="recon-1",
                           versao_correspondencia="match-1")


def _estado():
    """Registros publicados, sem instantes de publicação nem rastro de recálculo."""
    db.session.expire_all()
    return {(r.indicador, r.periodo_inicio, r.parada_codigo, r.trecho_id, r.versao_calculo):
            (r.valor, r.contagem_numerador, r.contagem_denominador)
            for r in IndicadorOperacional.query.all()}


def _brutos():
    db.session.expire_all()
    return ([(b.id, b.horario_gps, b.coletado_em, b.payload) for b in RegistroBrutoGPS.query.order_by("id")],
            [(t.bruto_id, t.valida, t.linha_codigo, t.sentido_gtfs)
             for t in ObservacaoTratada.query.order_by("bruto_id")])


@pytest.fixture
def referencia(tmp_path):
    """Reprocessamento MANUAL numa base limpa com os dados finais (agenda à mão)."""
    def montar(**dados):
        manual = create_app({"TESTING": True, "MYSQL_SSL_CA": None,
                             "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'referencia.db'}"})
        manual.instance_path = str(tmp_path / "referencia")
        with manual.app_context():
            try:
                db.create_all()
                _semear(**dados)
                consolidar_dia(DIA, CONSOLIDADO_EM)
                return _estado()
            finally:
                db.session.remove()
                db.engine.dispose()
    return montar


def _cumprimento(inicio, regra=VERSAO_ALGORITMO):
    return _estado()[("CUMPRIMENTO_VIAGENS", inicio, None, None, regra)]


# --- Gatilho 1: dado tardio -------------------------------------------------------------

@pytest.fixture
def tardio(app):
    """Publicado sem a viagem de 12:15; chega a posição tardia e a reconstrução a forma."""
    versao, partidas = _semear(tardia=False)
    consolidar_dia(DIA, CONSOLIDADO_EM)
    assert _cumprimento(T0 + M(15))[0] == 0.0  # conta à mão: 0 ÷ 1
    coleta = ExecucaoColeta(estado="OK", prevista_em=RECEBIDO_EM, iniciada_em=RECEBIDO_EM)
    bruto = RegistroBrutoGPS(execucao=coleta, horario_gps=T0 + M(20), coletado_em=RECEBIDO_EM,
                             payload={"p": "5810", "py": -23.5, "px": -46.6})
    db.session.add(bruto)
    db.session.flush()
    db.session.add(ObservacaoTratada(bruto_id=bruto.id, valida=True, versao_correspondencia="match-1",
                                     versao_gtfs_id=versao.id, linha_codigo="3762-10", sentido_gtfs="0"))
    db.session.add(_viagem(partidas[1], T0 + M(16), T0 + M(29)))  # reconstrução com o dado tardio
    db.session.commit()
    antes = {"estado": _estado(), "brutos": _brutos(), "versao": versao_vigente()["id"]}
    plano = planejar_tardio(bruto_id=bruto.id, versao_regra="agenda", lacuna=LACUNA, **VERSOES)
    pedidos = registrar_pedidos_do_plano(plano)
    assert pedidos and {p.parametros["origem"]["tipo"] for p in pedidos} == {"GPS_TARDIO"}
    return {**antes, "pedidos": sorted(p.id for p in pedidos)}


def test_tardio_chega_ao_resultado_manual_e_preserva_brutos(app, tardio, referencia):
    feitos = recalculo.executor_recalculo(app, relogio=lambda: WORKER).executar(uma_vez=True)
    assert sorted(feitos) == [(p, "PRONTO") for p in tardio["pedidos"]]
    manual = referencia(tardia=True)
    assert _estado() == manual
    assert _cumprimento(T0 + M(15)) == (100.0, 1, 1)  # conta à mão: 1 ÷ 1
    assert _brutos() == tardio["brutos"]
    assert versao_vigente()["id"] in tardio["pedidos"]


def test_reinicio_durante_o_job_retoma_e_chega_ao_mesmo_resultado(app, config, tardio, referencia):
    def morre(job):
        recalculo.calcular_pedido(job)
        raise Interrompido  # processo encerrado no meio do cálculo (SIGKILL)

    executor = recalculo.executor_recalculo(app, relogio=lambda: WORKER)
    executor.tarefa = morre
    with pytest.raises(Interrompido):
        executor.executar_um()
    assert _estado() == tardio["estado"]  # nada publicado pela metade
    db.session.remove()
    db.engine.dispose()  # fim do processo

    reiniciado = create_app(config)
    reiniciado.instance_path = app.instance_path
    depois = WORKER + JobService.RESERVA  # a reserva do processo morto venceu
    feitos = recalculo.executor_recalculo(reiniciado, relogio=lambda: depois).executar(uma_vez=True)
    assert sorted(feitos) == [(p, "PRONTO") for p in tardio["pedidos"]]
    assert _estado() == referencia(tardia=True)
    assert _brutos() == tardio["brutos"]
    historicos = [[t["estado"] for t in db.session.get(ExecucaoProcessamento, p).historico]
                  for p in tardio["pedidos"]]
    assert ["INTERROMPIDA", "PRONTO"] in historicos


def test_falha_antes_de_publicar_nao_expoe_parcial_e_a_retentativa_converge(app, tardio, referencia):
    publicados = []

    def publica_e_falha(job, resultados):
        recalculo.publicar_pedido(job, resultados)  # gravado na transação, sem commit
        publicados.append(job.id)
        raise RuntimeError("falha antes de confirmar")

    executor = recalculo.executor_recalculo(app, relogio=lambda: WORKER)
    executor.publicar = publica_e_falha
    pedido, estado = executor.executar_um()
    assert estado == "ERRO" and publicados == [pedido]
    assert _estado() == tardio["estado"]  # nenhum resultado parcial visível
    assert versao_vigente()["id"] == tardio["versao"]
    assert _cumprimento(T0 + M(15))[0] == 0.0
    assert _brutos() == tardio["brutos"]

    feitos = recalculo.executor_recalculo(app, relogio=lambda: WORKER + M(1)).executar(uma_vez=True)
    assert sorted(feitos) == [(p, "PRONTO") for p in tardio["pedidos"]]
    assert _estado() == referencia(tardia=True)
    historico = db.session.get(ExecucaoProcessamento, pedido).historico
    assert [(t["estado"], t["erro"]) for t in historico] == [("ERRO", "RuntimeError"), ("PRONTO", None)]


# --- Gatilho 2: mudança de regra --------------------------------------------------------

@pytest.fixture
def regra_antiga(app):
    """Conjunto publicado pela regra antiga: ela aceitava a viagem de 12:15 (cobertura 0,5)."""
    _semear(tardia=True, cobertura_tardia=0.5)
    consolidar_dia(DIA, CONSOLIDADO_EM)
    for registro in IndicadorOperacional.query.filter_by(indicador="CUMPRIMENTO_VIAGENS"):
        registro.versao_calculo = REGRA_ANTIGA
        registro.chave_resultado = f"{registro.chave_resultado}:{REGRA_ANTIGA}"
        if registro.periodo_inicio == T0 + M(15):
            registro.valor, registro.contagem_numerador = 100.0, 1  # regra antiga contava a viagem
    db.session.commit()
    return {"estado": _estado(), "brutos": _brutos()}


def test_regra_nova_chega_ao_resultado_manual_sem_misturar_regras(app, regra_antiga, referencia):
    assert _cumprimento(T0 + M(15), REGRA_ANTIGA)[0] == 100.0
    inicio, fim = periodo_utc(DIA)  # o dia de São Paulo inteiro (03:00–03:00 UTC)
    plano = planejar_regra(regra_anterior=REGRA_ANTIGA, inicio=inicio, fim=fim,
                           versao_regra=VERSAO_ALGORITMO, **VERSOES)
    pedidos = registrar_pedidos_do_plano(plano)
    assert pedidos and {p.versao_regra for p in pedidos} == {VERSAO_ALGORITMO}
    feitos = recalculo.executor_recalculo(app, relogio=lambda: WORKER).executar(uma_vez=True)
    assert {e for _, e in feitos} == {"PRONTO"}
    # Conta à mão pela regra vigente: cobertura 0,5 < 0,80 → 0 ÷ 1 = 0%.
    assert _cumprimento(T0 + M(15)) == (0.0, 0, 1)
    assert _estado() == referencia(tardia=True, cobertura_tardia=0.5)
    assert not [k for k in _estado() if k[4] == REGRA_ANTIGA]  # o painel não mostra as duas regras
    assert _brutos() == regra_antiga["brutos"]
