"""US16-T03: 20 casos com cálculo manual e agregações por contagens.

Todos os dados são SINTÉTICOS. Os horários são civis de America/Sao_Paulo,
fuso em que dia, mês e faixa horária são definidos. Os casos não representam
viagens reais da SPTrans nem foram aprovados pela Instituição Parceira.
"""

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.indicadores.pontualidade import (
    DiferencaSaida,
    ReferenciaProgramacao,
    ResultadoPontualidade,
    SaidaObservada,
    calcular_diferencas,
    calcular_percentual_pontualidade,
    publicar_pontualidade,
)
from app.models import ImportacaoProgramacao, IndicadorOperacional, PartidaProgramada
from app.viagens.separacao import ViagemObservada

SP = ZoneInfo("America/Sao_Paulo")
LINHA = "3762-10"  # mesma linha da fixture `programacao`
VERSAO_SEM_BANCO = "gtfs-sintetica-v1"


def sp(dia, hora, minuto):
    return datetime(2026, *dia, hora, minuto, tzinfo=SP)


# Janelas de 15 minutos [inicio, fim) em horário de São Paulo.
JANELAS = {
    "W1": (sp((1, 31), 7, 0), sp((1, 31), 7, 15)),    # faixa 06–09, 31/jan
    "W2": (sp((1, 30), 7, 15), sp((1, 30), 7, 30)),   # faixa 06–09, 30/jan
    "W3": (sp((1, 31), 23, 45), sp((2, 1), 0, 0)),    # noite de 31/jan
    "W4": (sp((2, 1), 7, 0), sp((2, 1), 7, 15)),      # fevereiro
    "W5": (sp((2, 1), 7, 15), sp((2, 1), 7, 30)),     # só exclusões
}

# caso, janela, minuto da partida na janela, entrada, status esperado, pontual (cálculo manual)
CASOS = (
    ("01", "W1", 1, -60, "CALCULADA", True),          # limite adiantado inclusivo
    ("02", "W1", 2, -60.001, "CALCULADA", False),     # 1 ms além do limite
    ("03", "W1", 3, -1, "CALCULADA", True),
    ("04", "W1", 4, 0, "CALCULADA", True),
    ("05", "W4", 1, 1, "CALCULADA", True),
    ("06", "W4", 2, 120, "CALCULADA", True),
    ("07", "W4", 3, 299.999, "CALCULADA", True),
    ("08", "W3", 1, 300, "CALCULADA", True),          # limite atrasado inclusivo
    ("09", "W2", 1, 300.001, "CALCULADA", False),     # 1 ms além do limite
    ("10", "W3", 2, -120, "CALCULADA", False),
    ("11", "W3", 5, 600, "CALCULADA", False),         # 23:50 + 10 min = 00:00 de 01/fev
    ("12", "W1", 5, "FREQUENCIA_PAREADA", "FREQUENCIA", False),
    ("13", "W2", 2, "SEM_PAREAMENTO", "SEM_PAREAMENTO", False),
    ("14", "W2", 3, "ID_DESCONHECIDO", "SEM_PAREAMENTO", False),
    ("15", "W2", 4, "SEM_SAIDA_CONFIRMADA", "SAIDA_NAO_CONFIRMADA", False),
    ("16", "W3", 3, "LINHA_DIVERGENTE", "SEM_PAREAMENTO", False),
    ("17", "W3", 4, "SENTIDO_DIVERGENTE", "SEM_PAREAMENTO", False),
    ("18", "W4", 4, "DIA_DIVERGENTE", "SEM_PAREAMENTO", False),
    ("19", "W5", 1, "VERSAO_DIVERGENTE", "SEM_PAREAMENTO", False),
    ("20", "W5", 2, "FREQUENCIA_SEM_PAREAMENTO", "FREQUENCIA", False),
)


def prevista(janela, minuto):
    return JANELAS[janela][0] + timedelta(minutes=minuto)


def dia_operacao(janela):
    # A operação é a do dia civil em que a partida está programada (SP).
    return JANELAS[janela][0].date()


def partida(caso_id, janela, minuto, versao, tipo="FIXA"):
    lote = ImportacaoProgramacao(
        id=f"import-{caso_id}", linha_codigo=LINHA, sentido="0",
        data_operacao=dia_operacao(janela), versao_gtfs_id=versao,
        tipo_dia="UTIL", fonte="OFICIAL", versao_fonte="captura-sintetica-v1",
        fonte_url="https://example.invalid/grade", capturada_em=JANELAS["W1"][0],
        verificada_em=JANELAS["W1"][0], arquivo_id=f"arquivo-{caso_id}",
    )
    return PartidaProgramada(
        id=f"p-{caso_id}", importacao=lote, ordem=1,
        saida_prevista=prevista(janela, minuto), tipo_oferta=tipo,
        intervalo_seg=900 if tipo == "FREQUENCIA" else None,
    )


def entrada(caso_id, janela, minuto, versao, delta=0):
    saida = prevista(janela, minuto) + timedelta(seconds=delta)
    viagem = ViagemObservada(
        prefixo=f"bus-{caso_id}", linha_codigo=LINHA, sentido_gtfs="0",
        inicio=saida, fim=saida + timedelta(minutes=40),
        motivo_encerramento="fim_da_amostra",
    )
    return SaidaObservada(f"v-{caso_id}", viagem, dia_operacao(janela), versao, saida, f"p-{caso_id}")


def executar_caso(caso_id, janela, minuto, dado, versao=VERSAO_SEM_BANCO):
    """Cada caso é calculado isoladamente, com sua própria grade de referência."""
    tipo = "FREQUENCIA" if str(dado).startswith("FREQUENCIA") else "FIXA"
    grade = partida(caso_id, janela, minuto, versao, tipo)
    observada = entrada(caso_id, janela, minuto, versao, dado if isinstance(dado, (int, float)) else 0)
    if dado in ("FREQUENCIA_SEM_PAREAMENTO", "SEM_PAREAMENTO"):
        observada = replace(observada, partida_programada_id=None)
    elif dado == "ID_DESCONHECIDO":
        observada = replace(observada, partida_programada_id="inexistente")
    elif dado == "SEM_SAIDA_CONFIRMADA":
        observada = replace(observada, saida_observada=None)
    elif dado == "LINHA_DIVERGENTE":
        observada = replace(observada, viagem=replace(observada.viagem, linha_codigo="8001-10"))
    elif dado == "SENTIDO_DIVERGENTE":
        observada = replace(observada, viagem=replace(observada.viagem, sentido_gtfs="1"))
    elif dado == "DIA_DIVERGENTE":
        observada = replace(observada, data_operacao=dia_operacao(janela) + timedelta(days=1))
    elif dado == "VERSAO_DIVERGENTE":
        observada = replace(observada, versao_gtfs_id=versao + "-outra")
    return calcular_diferencas([observada], [grade]).viagens[0]


@pytest.mark.parametrize("caso_id,janela,minuto,dado,status_manual,pontual_manual", CASOS)
def test_20_casos_comparam_calculo_manual_e_resultado_do_sistema(
    caso_id, janela, minuto, dado, status_manual, pontual_manual,
):
    item = executar_caso(caso_id, janela, minuto, dado)
    assert item.status == status_manual
    if isinstance(dado, (int, float)):
        assert item.diferenca_segundos == dado
        assert item.programacao.saida_prevista == prevista(janela, minuto)
    else:
        assert item.diferenca_segundos is None
    resultado = calcular_percentual_pontualidade([item])
    assert (resultado.pontuais, resultado.realizadas) == (
        int(pontual_manual), int(status_manual == "CALCULADA"))
    if status_manual != "CALCULADA":
        assert resultado.percentual is None and resultado.dados_insuficientes


def test_virada_de_dia_e_mes_usa_horario_de_sao_paulo_e_dia_operacional():
    # Inclui o antigo test_data_operacional_preserva_dia_e_mes_na_virada_de_calendario
    # da main, agora com horários de São Paulo.
    item = executar_caso("11", "W3", 5, 600)
    assert item.saida_observada.astimezone(SP) == sp((2, 1), 0, 0)
    assert item.saida_observada == datetime(2026, 2, 1, 3, 0, tzinfo=timezone.utc)
    assert item.data_operacao == item.programacao.data_operacao == date(2026, 1, 31)


# Cálculo manual por janela: (pontuais, realizadas, exclusões).
MANUAL_JANELAS = {
    "W1": (3, 4, {"frequencia": 1, "sem_pareamento": 0, "saida_nao_confirmada": 0}),
    "W2": (0, 1, {"frequencia": 0, "sem_pareamento": 2, "saida_nao_confirmada": 1}),
    "W3": (1, 3, {"frequencia": 0, "sem_pareamento": 2, "saida_nao_confirmada": 0}),
    "W4": (3, 3, {"frequencia": 0, "sem_pareamento": 1, "saida_nao_confirmada": 0}),
    "W5": (0, 0, {"frequencia": 1, "sem_pareamento": 1, "saida_nao_confirmada": 0}),
}


def _publicar(itens, inicio, fim, versao_id):
    return publicar_pontualidade(
        ResultadoPontualidade(tuple(itens)), linha=LINHA, sentido="0", inicio=inicio, fim=fim,
        versao_gtfs_id=versao_id, versao_correspondencia="corr-sintetica-t03",
        versao_reconstrucao="recon-sintetica-t03",
    )


def _soma(registros):
    return (sum(r.contagem_numerador for r in registros),
            sum(r.contagem_denominador for r in registros))


def test_janelas_faixa_dia_e_mes_agregam_por_contagens(app, programacao):
    versao, linha, _, _ = programacao
    assert linha.codigo == LINHA
    itens = {caso[0]: executar_caso(caso[0], caso[1], caso[2], caso[3], versao.id) for caso in CASOS}
    por_janela = {j: [itens[c[0]] for c in CASOS if c[1] == j] for j in JANELAS}

    publicados = {}
    for janela, (inicio, fim) in JANELAS.items():
        registro = _publicar(por_janela[janela], inicio, fim, versao.id)
        pontuais, realizadas, excluidas = MANUAL_JANELAS[janela]
        assert fim - inicio == timedelta(minutes=15)
        assert (registro.contagem_numerador, registro.contagem_denominador) == (pontuais, realizadas)
        assert registro.qualidade["excluidas"] == excluidas
        assert registro.unidade == "%" and registro.versao_calculo == "pontualidade-1.0"
        assert registro.qualidade["limites_inclusivos"] is True
        assert (registro.qualidade["limite_adiantamento_segundos"],
                registro.qualidade["limite_atraso_segundos"]) == (60, 300)
        publicados[janela] = registro

    # Percentuais por janela: 75%, 0%, 33,33%, 100% e NULL (dados insuficientes, não zero).
    assert [publicados[j].valor for j in JANELAS] == [75.0, 0.0, 33.33, 100.0, None]
    assert publicados["W5"].qualidade["dados_insuficientes"] is True

    # Faixa 06:00–09:00 de janeiro (W1 + W2, dias diferentes): 3/5 = 60%; média simples seria 37,5%.
    faixa = calcular_percentual_pontualidade(por_janela["W1"] + por_janela["W2"])
    assert (faixa.pontuais, faixa.realizadas, faixa.percentual) == (3, 5, Decimal("60.00"))
    assert _soma([publicados["W1"], publicados["W2"]]) == (3, 5)

    # Dia operacional 31/jan e meses: publicação direta da janela maior = soma das contagens.
    maiores = {
        "dia 31/jan": ((1, 31), (2, 1), ["W1", "W3"], (4, 7), 57.14),
        "jan": ((1, 1), (2, 1), ["W1", "W2", "W3"], (4, 8), 50.0),
        "fev": ((2, 1), (3, 1), ["W4", "W5"], (3, 3), 100.0),
    }
    for nome, (de, ate, janelas, contagens, percentual) in maiores.items():
        itens_periodo = [i for j in janelas for i in por_janela[j]]
        registro = _publicar(itens_periodo, sp(de, 0, 0), sp(ate, 0, 0), versao.id)
        assert _soma([publicados[j] for j in janelas]) == contagens, nome
        assert (registro.contagem_numerador, registro.contagem_denominador) == contagens, nome
        assert registro.valor == pytest.approx(percentual), nome

    # Média simples dos percentuais de janeiro (75, 0, 33,33) seria 36,11%, não 50%.
    media_simples = sum(publicados[j].valor for j in ("W1", "W2", "W3")) / 3
    assert media_simples == pytest.approx(36.11, abs=0.01)

    total = calcular_percentual_pontualidade(list(itens.values()))
    assert (total.pontuais, total.realizadas, total.percentual) == (7, 11, Decimal("63.64"))
    assert total.excluidas == {"frequencia": 2, "sem_pareamento": 6, "saida_nao_confirmada": 1}
    assert total.classificadas == 20


def test_agregacao_dos_20_casos_publicada_em_um_registro(app, programacao):
    versao, linha, _, _ = programacao
    itens = [executar_caso(caso[0], caso[1], caso[2], caso[3], versao.id) for caso in CASOS]

    # Verifica que percentuais combinados vêm das contagens: 1/1 e 1/3
    # resultam em 2/4 = 50%, não na média simples de 66,67%.
    particionais = ResultadoPontualidade(tuple(
        DiferencaSaida(
            f"v-{indice}", date(2026, 1, 31), "CALCULADA", "FIXA_PAREADA",
            sp((1, 31), 23, 50), None, delta,
        )
        for indice, delta in enumerate((0, 301, 301, 0), start=1)
    ))
    ponderado = calcular_percentual_pontualidade(particionais)
    assert (ponderado.pontuais, ponderado.realizadas, ponderado.percentual) == (
        2, 4, Decimal("50.00"),
    )

    # Um único registro com os 20 casos (30/jan 00:00 a 02/fev 00:00, SP).
    inicio, fim = sp((1, 30), 0, 0), sp((2, 2), 0, 0)
    registro = _publicar(itens, inicio, fim, versao.id)
    assert registro.periodo_inicio == inicio
    assert registro.periodo_fim == fim
    assert (registro.contagem_numerador, registro.contagem_denominador) == (7, 11)
    assert registro.valor == pytest.approx(63.64)
    assert registro.qualidade["excluidas"] == {
        "frequencia": 2, "sem_pareamento": 6, "saida_nao_confirmada": 1,
    }
    assert IndicadorOperacional.query.filter_by(id=registro.id).count() == 1


def _calculada(viagem_id, prevista_sp, delta, versao_id):
    """Viagem fixa pareada do dia operacional 31/jan, com programação de referência."""
    ref = ReferenciaProgramacao(
        partida_id=f"p-{viagem_id}", importacao_id="import-virada", linha_codigo=LINHA,
        sentido_gtfs="0", data_operacao=date(2026, 1, 31), versao_gtfs_id=versao_id,
        tipo_oferta="FIXA", fonte="OFICIAL", versao_fonte="captura-sintetica-v1",
        fonte_url="https://example.invalid/grade", saida_prevista=prevista_sp,
    )
    return DiferencaSaida(
        viagem_id, date(2026, 1, 31), "CALCULADA", "FIXA_PAREADA",
        prevista_sp + timedelta(seconds=delta), ref, delta,
    )


def test_janelas_de_15_minutos_e_agregacao_por_contagens_no_limite_do_mes(app, programacao):
    versao, _, _, _ = programacao
    inicio_janela_1, fim_janela_1 = sp((1, 31), 23, 45), sp((2, 1), 0, 0)
    inicio_janela_2, fim_janela_2 = fim_janela_1, sp((2, 1), 0, 15)
    assert fim_janela_1 - inicio_janela_1 == timedelta(minutes=15)
    assert fim_janela_2 - inicio_janela_2 == timedelta(minutes=15)

    # Operação de 31/jan pode ter saída após a meia-noite civil.
    primeira_janela = ResultadoPontualidade((
        _calculada("j1-v1", sp((1, 31), 23, 50), 0, versao.id),
        _calculada("j1-v2", sp((1, 31), 23, 55), 301, versao.id),
    ))
    segunda_janela = ResultadoPontualidade((
        _calculada("j2-v1", sp((2, 1), 0, 5), 0, versao.id),
    ))
    registro_janela_1 = _publicar(primeira_janela.viagens, inicio_janela_1, fim_janela_1, versao.id)
    registro_janela_2 = _publicar(segunda_janela.viagens, inicio_janela_2, fim_janela_2, versao.id)
    assert (registro_janela_1.contagem_numerador,
            registro_janela_1.contagem_denominador, registro_janela_1.valor) == (1, 2, 50.0)
    assert (registro_janela_2.contagem_numerador,
            registro_janela_2.contagem_denominador, registro_janela_2.valor) == (1, 1, 100.0)
    assert registro_janela_1.periodo_fim == registro_janela_2.periodo_inicio

    # 2/3 = 66,67%, e não a média simples de 75%.
    agregado_dia_operacional = calcular_percentual_pontualidade(
        primeira_janela.viagens + segunda_janela.viagens,
    )
    assert (agregado_dia_operacional.pontuais,
            agregado_dia_operacional.realizadas,
            agregado_dia_operacional.percentual) == (2, 3, Decimal("66.67"))
    assert {item.data_operacao for item in primeira_janela.viagens + segunda_janela.viagens} == {
        date(2026, 1, 31),
    }
