"""US23-T03 com relógio controlado e dados SINTÉTICOS (linha 3762-10, 24/11/2026).

Partidas às 12:00, 12:15 e 12:30 UTC (09:00 em São Paulo); a primeira tem
viagem reconstruída pontual. Nada acessa rede ou MySQL.
"""

from datetime import date, datetime, timedelta, timezone

import pytest

from app import db
from app.gtfs.scheduler import executar_tarefas
from app.indicadores import agenda
from app.indicadores.agenda import (
    ATUALIZACAO, CONSOLIDACAO, atualizar_indicadores, consolidar_dia, janela_fechada,
)
from app.models import ExecucaoProcessamento, IndicadorOperacional, ViagemObservada

UTC = timezone.utc
T0 = datetime(2026, 11, 24, 12, tzinfo=UTC)


@pytest.fixture
def viagens(programacao):
    _, _, _, partidas = programacao
    db.session.add(ViagemObservada(
        partida_programada=partidas[0], saida_observada=partidas[0].saida_prevista + timedelta(seconds=30),
        cobertura_tracado=0.9, versao_reconstrucao="recon-1", versao_correspondencia="match-1"))
    db.session.commit()
    return partidas


def execucoes(tipo):
    return (ExecucaoProcessamento.query.filter_by(tipo=tipo)
            .order_by(ExecucaoProcessamento.periodo_inicio).all())


def cumprimento(inicio):
    return IndicadorOperacional.query.filter_by(
        indicador="CUMPRIMENTO_VIAGENS", periodo_inicio=inicio).one_or_none()


def test_janela_fechada_e_a_ultima_de_15_minutos():
    assert janela_fechada(datetime(2026, 11, 24, 12, 14, 59, tzinfo=UTC)) == (
        datetime(2026, 11, 24, 11, 45, tzinfo=UTC), T0)
    assert janela_fechada(T0) == (T0 - timedelta(minutes=15), T0)
    with pytest.raises(ValueError):
        janela_fechada(datetime(2026, 11, 24, 12))


def test_relogio_controlado_comprova_15_minutos_e_consolidacao_ate_06h(viagens, app):
    relogio = [T0 + timedelta(seconds=30)]
    fim = datetime(2026, 11, 25, 10, tzinfo=UTC)  # 07:00 em São Paulo

    def avancar(_prevista=None, relogio_=None, **_):
        relogio[0] += timedelta(seconds=60)

    executar_tarefas(relogio=lambda: relogio[0], dormir=lambda s: avancar(),
                     verificar=lambda prevista, relogio=None: avancar(),
                     manter_ativo=lambda: relogio[0] < fim, periodicas=(atualizar_indicadores,))

    atualizacoes = execucoes(ATUALIZACAO)
    # Uma por janela fechada, de 11:45–12:00 até a última antes do fim do relógio.
    assert len(atualizacoes) == (fim - T0) // timedelta(minutes=15)
    assert [a.periodo_inicio for a in atualizacoes] == [
        T0 - timedelta(minutes=15) + i * timedelta(minutes=15) for i in range(len(atualizacoes))]
    for execucao in atualizacoes:
        assert execucao.estado == "PRONTO"
        # Publicada no primeiro minuto depois que a janela fechou.
        assert execucao.periodo_fim <= execucao.finalizada_em < execucao.periodo_fim + timedelta(minutes=1)
    assert cumprimento(T0).valor == pytest.approx(100.0)
    assert cumprimento(T0 + timedelta(minutes=15)).valor == 0.0  # programada sem viagem

    consolidacao = next(c for c in execucoes(CONSOLIDACAO) if c.periodo_inicio.day == 24)
    assert consolidacao.periodo_inicio == datetime(2026, 11, 24, 3, tzinfo=UTC)  # 24/11 00:00 local
    assert consolidacao.periodo_fim == datetime(2026, 11, 25, 3, tzinfo=UTC)
    # Não antes de 04:00 e dentro do prazo de 06:00 de 25/11 (07:00–09:00 UTC).
    assert datetime(2026, 11, 25, 7, tzinfo=UTC) <= consolidacao.finalizada_em
    assert consolidacao.finalizada_em < datetime(2026, 11, 25, 9, tzinfo=UTC)
    assert consolidacao.parametros["no_prazo"] is True
    assert consolidacao.parametros["janelas"] == 96


def test_falha_no_meio_nao_publica_conjunto_parcial(viagens, app, monkeypatch):
    consolidar_dia(date(2026, 11, 24), datetime(2026, 11, 25, 7, 1, tzinfo=UTC))
    antes = {(r.indicador, r.periodo_inicio): (r.valor, r.publicado_em) for r in IndicadorOperacional.query}
    painel_antes = app.test_client().get("/series", query_string={
        "linha": "3762-10", "sentido": "ida", "tipo": "CUMPRIMENTO_VIAGENS"}).get_json()

    # Nova viagem muda o resultado, mas o cálculo falha no meio da atualização.
    db.session.add(ViagemObservada(
        partida_programada=viagens[1], saida_observada=viagens[1].saida_prevista,
        cobertura_tracado=0.9, versao_reconstrucao="recon-1", versao_correspondencia="match-1"))
    db.session.commit()
    chamadas = []

    def quebra(**parametros):
        chamadas.append(parametros["inicio"])
        if len(chamadas) > 3:
            raise RuntimeError("queda no meio")
        return agenda.calcular_indicador_cumprimento(**parametros)

    monkeypatch.setattr(agenda, "CALCULOS", (agenda.calcular_indicador_cumprimento, quebra))
    with pytest.raises(RuntimeError):
        consolidar_dia(date(2026, 11, 25), datetime(2026, 11, 26, 7, 1, tzinfo=UTC))
    with pytest.raises(RuntimeError):
        # Força a reconsolidação do dia 24 com o novo dado, também interrompida.
        ExecucaoProcessamento.query.filter_by(tipo=CONSOLIDACAO).delete()
        db.session.commit()
        consolidar_dia(date(2026, 11, 24), datetime(2026, 11, 25, 7, 30, tzinfo=UTC))

    depois = {(r.indicador, r.periodo_inicio): (r.valor, r.publicado_em) for r in IndicadorOperacional.query}
    assert depois == antes  # nenhum registro do conjunto interrompido ficou visível
    erro = ExecucaoProcessamento.query.filter_by(
        chave_logica=f"US23-T03:{CONSOLIDACAO}:2026-11-24").one()
    assert (erro.estado, erro.erro) == ("ERRO", "RuntimeError")
    painel = app.test_client().get("/series", query_string={
        "linha": "3762-10", "sentido": "ida", "tipo": "CUMPRIMENTO_VIAGENS"}).get_json()
    assert [p["valor"] for p in painel["dados"]] == [p["valor"] for p in painel_antes["dados"]]


def test_contrato_informa_atualizacao_e_consolidacao(viagens, app):
    atualizar_indicadores(T0 + timedelta(minutes=16))  # janela 12:00–12:15
    cliente = app.test_client()
    consulta = {"linha": "3762-10", "sentido": "ida", "tipo": "CUMPRIMENTO_VIAGENS"}
    corpo = cliente.get("/series", query_string=consulta).get_json()
    estado = corpo["atualizacao"]
    assert estado["intervalo_minutos"] == 15
    assert estado["ultima_atualizacao"] == {
        "janela_inicio": "2026-11-24T12:00:00+00:00", "janela_fim": "2026-11-24T12:15:00+00:00",
        "publicada_em": "2026-11-24T12:16:00+00:00"}
    assert estado["proxima_atualizacao_prevista"] == "2026-11-24T12:30:00+00:00"
    # 09:16 em São Paulo: o dia anterior (23/11) já foi consolidado; o 24/11 ainda não.
    assert estado["consolidacao"]["dia"] == "2026-11-23"
    assert [p["consolidado"] for p in corpo["dados"]] == [False]

    consolidar_dia(date(2026, 11, 24), datetime(2026, 11, 25, 7, 5, tzinfo=UTC))
    corpo = cliente.get("/series", query_string=consulta).get_json()
    assert corpo["atualizacao"]["consolidacao"] | {"regra": None} == {
        "dia": "2026-11-24", "consolidado_ate": "2026-11-25T03:00:00+00:00",
        "consolidado_em": "2026-11-25T07:05:00+00:00", "no_prazo": True, "regra": None}
    assert len(corpo["dados"]) == 96
    assert all(p["consolidado"] and p["consolidado_em"] == "2026-11-25T07:05:00+00:00"
               for p in corpo["dados"])


def test_reexecucao_e_idempotente_e_sem_viagem_nao_publica(programacao, app):
    assert atualizar_indicadores(T0 + timedelta(minutes=16))
    assert IndicadorOperacional.query.count() == 0  # sem reconstrução, nada de 0% falso
    atualizar_indicadores(T0 + timedelta(minutes=17))
    assert len(execucoes(ATUALIZACAO)) == 1
