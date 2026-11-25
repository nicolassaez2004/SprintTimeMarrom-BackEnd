"""Review US23-T03 × US17/US18 com relógio controlado e dados SINTÉTICOS.

Cenário da US17 (parada B, passagens 12:00, 12:04, 12:10, 12:13 e 12:16 UTC de
24/11/2026, H = 300 s): a agenda deve recalcular regularidade e espera, gravar
só as janelas da publicação, substituir conjuntos antigos na mesma transação e
informar ``consolidado`` apenas para indicadores que a consolidação recalculou.
"""

from datetime import date, datetime, timedelta, timezone

import pytest

from app import db
from app.indicadores import agenda
from app.indicadores.agenda import (
    ATUALIZACAO, CONSOLIDACAO, atualizar_indicadores, atualizar_janela, consolidar_dia,
)
from app.indicadores.espera import ESPERA, EXCESSO
from app.indicadores.regularidade import REGULARIDADE
from app.models import ExecucaoProcessamento, IndicadorOperacional
from test_regularidade_us17 import (  # noqa: F401 (fixtures)
    MANUAL, T0, cenario, frequencia, lacuna_de_teste, passar,
)

UTC = timezone.utc
DIA = date(2026, 11, 24)
CONSOLIDA = datetime(2026, 11, 25, 7, 5, tzinfo=UTC)  # 04:05 de 25/11 em São Paulo


@pytest.fixture(autouse=True)
def coleta_de_60s(app):
    app.config["GPS_COLLECTION_INTERVAL_SECONDS"] = 60  # ciclos do cenário sintético


def registros(indicador, parada="B", inicio=T0):
    return IndicadorOperacional.query.filter_by(
        indicador=indicador, parada_codigo=parada, periodo_inicio=inicio).all()


def test_consolidacao_recalcula_regularidade_espera_e_excesso(frequencia, app):
    passar(frequencia, "B", MANUAL)
    execucao = consolidar_dia(DIA, CONSOLIDA)
    assert {REGULARIDADE, ESPERA, EXCESSO} <= set(execucao.parametros["indicadores"])
    (cv,), (espera,), (excesso,) = registros(REGULARIDADE), registros(ESPERA), registros(EXCESSO)
    # Mesmos valores da conta manual (docs/US17.md e docs/US18.md).
    assert cv.valor == pytest.approx(0.287820, abs=1e-6)
    assert espera.valor == pytest.approx(2.346154, abs=1e-6)
    assert excesso.valor == pytest.approx(-0.153846, abs=1e-6)
    assert {cv.publicado_em, espera.publicado_em} == {CONSOLIDA}
    # Nada fora do dia civil 24/11 em São Paulo é gravado por esta consolidação.
    assert all(execucao.periodo_inicio <= r.periodo_inicio and r.periodo_fim <= execucao.periodo_fim
               for r in IndicadorOperacional.query)


def test_api_series_e_indicadores_marcam_consolidado_por_indicador(frequencia, app):
    passar(frequencia, "B", MANUAL)
    cliente = app.test_client()
    serie = {"linha": "3762-10", "sentido": "ida", "parada": "B", "tipo": REGULARIDADE,
             "inicio": T0.isoformat(), "fim": (T0 + timedelta(minutes=15)).isoformat()}
    atualizar_janela(T0 + timedelta(minutes=16))
    (ponto,) = cliente.get("/series", query_string=serie).get_json()["dados"]
    assert (ponto["valor"], ponto["consolidado"]) == (pytest.approx(0.28782, abs=1e-6), False)

    consolidar_dia(DIA, CONSOLIDA)
    (ponto,) = cliente.get("/series", query_string=serie).get_json()["dados"]
    assert ponto["consolidado"] is True and ponto["consolidado_em"] == CONSOLIDA.isoformat()
    dia = cliente.get("/indicadores", query_string={
        "linha": "3762-10", "sentido": "ida", "parada": "B", "tipo": "ESPERA", "agregacao": "dia",
        "inicio": "2026-11-24T00:00:00-03:00", "fim": "2026-11-25T00:00:00-03:00"}).get_json()
    assert dia["atualizacao"]["consolidacao"]["dia"] == "2026-11-24"
    assert [i["consolidado"] for i in dia["dados"]] == [True]

    # Uma consolidação antiga (v1) só recalculava US14/US16: a regularidade não é consolidada.
    ExecucaoProcessamento.query.filter_by(tipo=CONSOLIDACAO).one().parametros = {"no_prazo": True}
    db.session.commit()
    (ponto,) = cliente.get("/series", query_string=serie).get_json()["dados"]
    assert ponto["consolidado"] is False


def test_atualizacao_de_15_min_grava_so_a_janela_fechada(frequencia, app):
    passar(frequencia, "B", MANUAL)
    atualizar_janela(T0 + timedelta(minutes=16))  # janela 12:00–12:15 UTC
    periodos = {(r.periodo_inicio, r.periodo_fim) for r in IndicadorOperacional.query}
    assert periodos == {(T0, T0 + timedelta(minutes=15))}
    assert registros(REGULARIDADE)[0].valor == pytest.approx(0.287820, abs=1e-6)


def test_consolidacao_substitui_conjunto_antigo_sem_misturar(frequencia, app):
    passar(frequencia, "B", MANUAL)
    atualizar_janela(T0 + timedelta(minutes=16))
    antigo = registros(REGULARIDADE)[0].versao_conjunto
    # A US04 recapturou a programação do dia: o conjunto muda (novas versões).
    frequencia["importacao"].versao_fonte = "captura-2"
    db.session.commit()
    execucao = consolidar_dia(DIA, CONSOLIDA)
    for indicador in (REGULARIDADE, ESPERA, EXCESSO):
        (unico,) = registros(indicador)
        assert unico.versao_conjunto != antigo
    (cumprimento,) = IndicadorOperacional.query.filter_by(
        indicador="CUMPRIMENTO_VIAGENS", periodo_inicio=T0).all()
    assert cumprimento.qualidade["capturas_programacao"]
    assert execucao.parametros["substituidos"] > 0
    conjuntos = {r.versao_conjunto for r in IndicadorOperacional.query.filter_by(indicador=REGULARIDADE)}
    assert antigo not in conjuntos


def test_janelas_apos_24h_entram_na_consolidacao_do_dia_civil_seguinte(frequencia, app):
    # Passagens 00:05, 00:10 e 00:20 de 25/11 (São Paulo) de viagens da data 24/11 (GTFS > 24h).
    passar(frequencia, "B", (905, 910, 920))
    meia_noite = datetime(2026, 11, 25, 3, tzinfo=UTC)
    consolidar_dia(DIA, CONSOLIDA)
    assert registros(REGULARIDADE, inicio=meia_noite) == []
    consolidar_dia(date(2026, 11, 25), datetime(2026, 11, 26, 7, 5, tzinfo=UTC))
    (apos,) = [r for r in registros(REGULARIDADE, inicio=meia_noite)
               if r.qualidade["data_operacao"] == "2026-11-24"]
    assert apos.contagem_numerador is None or apos.contagem_numerador >= 0


def test_falha_no_calculo_diario_desfaz_a_publicacao_inteira(frequencia, app, monkeypatch):
    passar(frequencia, "B", MANUAL)

    class Quebra(agenda.CalculoEspera):
        def calcular(self):
            raise RuntimeError("queda no meio")

    monkeypatch.setattr(agenda, "DIARIOS", (agenda.CalculoFrequencia, agenda.CalculoRegularidade, Quebra))
    with pytest.raises(RuntimeError):
        consolidar_dia(DIA, CONSOLIDA)
    assert IndicadorOperacional.query.count() == 0  # nem cumprimento nem regularidade parciais
    erro = ExecucaoProcessamento.query.filter_by(tipo=CONSOLIDACAO).one()
    assert (erro.estado, erro.erro) == ("ERRO", "RuntimeError")
    monkeypatch.undo()
    assert consolidar_dia(DIA, CONSOLIDA).estado == "PRONTO"  # retomada na volta seguinte
    assert registros(REGULARIDADE)[0].valor == pytest.approx(0.287820, abs=1e-6)


@pytest.mark.parametrize("hora, minuto, consolida, no_prazo", [
    (0, 0, False, None), (3, 59, False, None), (4, 0, True, True),
    (5, 59, True, True), (6, 0, True, True), (6, 1, True, False),
])
def test_horarios_limite_de_sao_paulo(programacao, app, hora, minuto, consolida, no_prazo):
    agora = datetime(2026, 11, 25, hora, minuto, tzinfo=agenda.FUSO)
    resultado = atualizar_indicadores(agora)
    (atualizacao,) = ExecucaoProcessamento.query.filter_by(tipo=ATUALIZACAO).all()
    assert atualizacao.periodo_fim <= agora < atualizacao.periodo_fim + timedelta(minutes=15)
    consolidacoes = ExecucaoProcessamento.query.filter_by(tipo=CONSOLIDACAO).all()
    assert ("consolidacao" in resultado) is consolida
    if consolida:
        (c,) = consolidacoes
        assert c.periodo_inicio == datetime(2026, 11, 24, tzinfo=agenda.FUSO)
        assert c.parametros["no_prazo"] is no_prazo
    else:
        assert consolidacoes == []
    if (hora, minuto) == (0, 0):  # 00:00: fecha a janela 23:45–00:00 do dia anterior
        assert atualizacao.periodo_inicio == datetime(2026, 11, 24, 23, 45, tzinfo=agenda.FUSO)


def test_csv_publicado_pela_agenda_confere_com_a_api_us17_us18(frequencia, app):
    import csv
    import io

    passar(frequencia, "B", MANUAL)
    resultado = atualizar_indicadores(CONSOLIDA)  # consolida 24/11 e publica o CSV
    assert resultado["csv_indicadores"]["estado"] == "PUBLICADO"
    cliente = app.test_client()
    base = {"formato": "csv", "data": "2026-11-24", "linha": "3762-10", "sentido": "ida", "parada": "B"}
    janela_t0 = {"inicio": T0.isoformat(), "fim": T0.isoformat()}
    api = {"linha": "3762-10", "sentido": "ida", "parada": "B", **janela_t0}

    def linhas_csv(tipo):
        resposta = cliente.get("/downloads/indicadores", query_string={**base, "tipo": tipo})
        assert resposta.status_code == 200 and resposta.headers["X-Licenca"] == "CC BY 4.0"
        return list(csv.DictReader(io.StringIO(resposta.data.decode("utf-8-sig"))))

    def valor(texto):
        return None if texto == "" else float(texto)

    (cv_api,) = cliente.get("/indicadores", query_string={**api, "tipo": REGULARIDADE}).get_json()["dados"]
    cv_csv = {r["inicio"]: r for r in linhas_csv(REGULARIDADE)}
    assert valor(cv_csv[T0.isoformat()]["valor"]) == cv_api["valor"]
    assert cv_csv[T0.isoformat()]["versao_conjunto"] == cv_api["versao_conjunto"]
    insuficiente = cv_csv[(T0 + timedelta(minutes=15)).isoformat()]
    assert insuficiente["valor"] == "" and insuficiente["unidade"] == "CV (adimensional)"  # NULL ≠ 0

    (espera_api,) = cliente.get("/indicadores", query_string={**api, "tipo": "ESPERA"}).get_json()["dados"]
    espera_csv = {(r["tipo"], r["inicio"]): r for r in linhas_csv("ESPERA")}
    for tipo, lado in ((ESPERA, "espera_estimada"), (EXCESSO, "excesso_espera")):
        linha = espera_csv[(tipo, T0.isoformat())]
        assert valor(linha["valor"]) == espera_api[lado]["valor"]
        assert linha["unidade"] == espera_api[lado]["unidade"] == "min"
    assert valor(espera_csv[(EXCESSO, T0.isoformat())]["valor"]) < 0  # negativo preservado
