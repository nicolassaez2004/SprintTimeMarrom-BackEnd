"""US16-T03: twenty manually specified cases and published aggregations.

All inputs are synthetic. They verify calculations and do not represent
institutionally approved or observed SPTrans journeys.
"""

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

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

UTC = timezone.utc
DIA_OPERACIONAL = date(2026, 1, 31)
PREVISTA = datetime(2026, 1, 31, 23, 50, tzinfo=UTC)

# ID, delta seconds, expected status, manually calculated punctual outcome.
# Special rows encode explicit source/context exclusions instead of a delta.
CASOS = (
    ("01", -60, "CALCULADA", True),
    ("02", -60.001, "CALCULADA", False),
    ("03", -1, "CALCULADA", True),
    ("04", 0, "CALCULADA", True),
    ("05", 1, "CALCULADA", True),
    ("06", 120, "CALCULADA", True),
    ("07", 299.999, "CALCULADA", True),
    ("08", 300, "CALCULADA", True),
    ("09", 300.001, "CALCULADA", False),
    ("10", -120, "CALCULADA", False),
    ("11", 600, "CALCULADA", False),
    ("12", "FREQUENCIA_PAREADA", "FREQUENCIA", False),
    ("13", "SEM_PAREAMENTO", "SEM_PAREAMENTO", False),
    ("14", "ID_DESCONHECIDO", "SEM_PAREAMENTO", False),
    ("15", "SEM_SAIDA_CONFIRMADA", "SAIDA_NAO_CONFIRMADA", False),
    ("16", "LINHA_DIVERGENTE", "SEM_PAREAMENTO", False),
    ("17", "SENTIDO_DIVERGENTE", "SEM_PAREAMENTO", False),
    ("18", "DIA_DIVERGENTE", "SEM_PAREAMENTO", False),
    ("19", "VERSAO_DIVERGENTE", "SEM_PAREAMENTO", False),
    ("20", "FREQUENCIA_SEM_PAREAMENTO", "FREQUENCIA", False),
)


def partida(
    caso_id, tipo="FIXA", *, linha_codigo="8000-10", versao_gtfs_id="gtfs-v1",
    importacao_id=None,
):
    lote = ImportacaoProgramacao(
        id=importacao_id or f"import-{caso_id}", linha_codigo=linha_codigo, sentido="0",
        data_operacao=DIA_OPERACIONAL, versao_gtfs_id=versao_gtfs_id,
        tipo_dia="UTIL", fonte="OFICIAL", versao_fonte="captura-v1",
        fonte_url="https://example.invalid/grade", capturada_em=PREVISTA,
        verificada_em=PREVISTA, arquivo_id=f"arquivo-{caso_id}",
    )
    return PartidaProgramada(
        id=f"p-{caso_id}", importacao=lote, ordem=1,
        saida_prevista=PREVISTA, tipo_oferta=tipo,
        intervalo_seg=900 if tipo == "FREQUENCIA" else None,
    )


def entrada(caso_id, delta=None, *, linha_codigo="8000-10", versao_gtfs_id="gtfs-v1"):
    inicio = PREVISTA if delta is None else PREVISTA + timedelta(seconds=delta)
    viagem = ViagemObservada(
        prefixo=f"bus-{caso_id}", linha_codigo=linha_codigo, sentido_gtfs="0",
        inicio=PREVISTA, fim=PREVISTA + timedelta(minutes=20),
        motivo_encerramento="fim_da_amostra",
    )
    return SaidaObservada(
        f"v-{caso_id}", viagem, DIA_OPERACIONAL, versao_gtfs_id, inicio, f"p-{caso_id}",
    )


def executar_caso(
    caso_id, dado, *, linha_codigo="8000-10", versao_gtfs_id="gtfs-v1",
    importacao_id=None,
):
    opcoes_partida = {
        "linha_codigo": linha_codigo,
        "versao_gtfs_id": versao_gtfs_id,
        "importacao_id": importacao_id,
    }
    opcoes_entrada = {"linha_codigo": linha_codigo, "versao_gtfs_id": versao_gtfs_id}
    if isinstance(dado, (int, float)):
        grade = partida(caso_id, **opcoes_partida)
        observada = entrada(caso_id, dado, **opcoes_entrada)
    elif dado == "FREQUENCIA_PAREADA":
        grade = partida(caso_id, "FREQUENCIA", **opcoes_partida)
        observada = entrada(caso_id, 0, **opcoes_entrada)
    elif dado == "FREQUENCIA_SEM_PAREAMENTO":
        grade = partida(caso_id, "FREQUENCIA", **opcoes_partida)
        observada = replace(entrada(caso_id, 0, **opcoes_entrada), partida_programada_id=None)
    elif dado == "SEM_PAREAMENTO":
        grade = partida(caso_id, **opcoes_partida)
        observada = replace(entrada(caso_id, 0, **opcoes_entrada), partida_programada_id=None)
    elif dado == "ID_DESCONHECIDO":
        grade = partida(caso_id, **opcoes_partida)
        observada = replace(entrada(caso_id, 0, **opcoes_entrada), partida_programada_id="inexistente")
    elif dado == "SEM_SAIDA_CONFIRMADA":
        grade = partida(caso_id, **opcoes_partida)
        observada = replace(entrada(caso_id, 0, **opcoes_entrada), saida_observada=None)
    else:
        grade = partida(caso_id, **opcoes_partida)
        observada = entrada(caso_id, 0, **opcoes_entrada)
        if dado == "LINHA_DIVERGENTE":
            observada = replace(observada, viagem=replace(observada.viagem, linha_codigo="8001-10"))
        elif dado == "SENTIDO_DIVERGENTE":
            observada = replace(observada, viagem=replace(observada.viagem, sentido_gtfs="1"))
        elif dado == "DIA_DIVERGENTE":
            observada = replace(observada, data_operacao=date(2026, 2, 1))
        elif dado == "VERSAO_DIVERGENTE":
            observada = replace(observada, versao_gtfs_id="gtfs-v2")
        else:
            raise AssertionError(f"Caso não definido: {dado}")
    resultado = calcular_diferencas([observada], [grade])
    return resultado.viagens[0]


@pytest.mark.parametrize("caso_id,dado,status_manual,pontual_manual", CASOS)
def test_20_casos_comparam_calculo_manual_e_resultado_do_sistema(
    caso_id, dado, status_manual, pontual_manual,
):
    item = executar_caso(caso_id, dado)
    assert item.status == status_manual
    if isinstance(dado, (int, float)):
        assert item.diferenca_segundos == dado
    else:
        assert item.diferenca_segundos is None
    resultado = calcular_percentual_pontualidade([item])
    assert resultado.pontuais == int(pontual_manual)
    assert resultado.realizadas == int(status_manual == "CALCULADA")


def test_agregacao_dos_20_casos_usa_contagens_e_registra_exclusoes(app, programacao):
    versao, linha, _, _ = programacao
    itens = [
        executar_caso(
            caso_id, dado, linha_codigo=linha.codigo,
            versao_gtfs_id=versao.id, importacao_id="import-batch",
        )
        for caso_id, dado, _, _ in CASOS
    ]
    agregado = calcular_percentual_pontualidade(itens)

    # Manual: 7 pontuais / 11 viagens fixas classificáveis = 63,64%.
    assert (agregado.pontuais, agregado.realizadas, agregado.percentual) == (
        7, 11, Decimal("63.64"),
    )
    assert agregado.excluidas == {
        "frequencia": 2,
        "sem_pareamento": 6,
        "saida_nao_confirmada": 1,
    }
    assert agregado.classificadas == 20

    # Verifica que percentuais combinados vêm das contagens: 1/1 e 1/3
    # resultam em 2/4 = 50%, não na média simples de 66,67%.
    particionais = ResultadoPontualidade(tuple(
        DiferencaSaida(
            f"v-{indice}", DIA_OPERACIONAL, "CALCULADA", "FIXA_PAREADA",
            PREVISTA, None, delta,
        )
        for indice, delta in enumerate((0, 301, 301, 0), start=1)
    ))
    ponderado = calcular_percentual_pontualidade(particionais)
    assert (ponderado.pontuais, ponderado.realizadas, ponderado.percentual) == (
        2, 4, Decimal("50.00"),
    )

    inicio = datetime(2026, 1, 31, 23, 40, tzinfo=UTC)
    fim = datetime(2026, 2, 1, 0, 5, tzinfo=UTC)
    registro = publicar_pontualidade(
        ResultadoPontualidade(tuple(itens)), linha=linha.codigo, sentido="0",
        inicio=inicio, fim=fim, versao_gtfs_id=versao.id,
        versao_correspondencia="match-t03-v1", versao_reconstrucao="recon-t03-v1",
    )
    assert registro.periodo_inicio == inicio
    assert registro.periodo_fim == fim
    assert (registro.contagem_numerador, registro.contagem_denominador) == (7, 11)
    assert registro.valor == pytest.approx(63.64)
    assert registro.qualidade["excluidas"] == agregado.excluidas
    assert IndicadorOperacional.query.filter_by(id=registro.id).count() == 1


def test_janelas_de_15_minutos_e_agregacao_por_contagens_no_limite_do_mes(app, programacao):
    versao, linha, _, _ = programacao
    inicio_janela_1 = datetime(2026, 1, 31, 23, 45, tzinfo=UTC)
    fim_janela_1 = datetime(2026, 2, 1, 0, 0, tzinfo=UTC)
    inicio_janela_2 = fim_janela_1
    fim_janela_2 = datetime(2026, 2, 1, 0, 15, tzinfo=UTC)
    assert fim_janela_1 - inicio_janela_1 == timedelta(minutes=15)
    assert fim_janela_2 - inicio_janela_2 == timedelta(minutes=15)

    def referencia(partida_id, prevista, versao, linha):
        return ReferenciaProgramacao(
            partida_id=partida_id, importacao_id="import-dia-operacional",
            linha_codigo=linha.codigo, sentido_gtfs="0",
            data_operacao=date(2026, 1, 31), versao_gtfs_id=versao.id,
            tipo_oferta="FIXA", fonte="OFICIAL", versao_fonte="captura-teste",
            fonte_url="https://example.invalid/grade", saida_prevista=prevista,
        )

    # Operação de 31/jan pode ter saída após a meia-noite civil.
    primeira_janela = ResultadoPontualidade((
        DiferencaSaida(
            "j1-v1", date(2026, 1, 31), "CALCULADA", "FIXA_PAREADA",
            datetime(2026, 1, 31, 23, 50, tzinfo=UTC),
            referencia("p-j1-v1", datetime(2026, 1, 31, 23, 50, tzinfo=UTC), versao, linha), 0,
        ),
        DiferencaSaida(
            "j1-v2", date(2026, 1, 31), "CALCULADA", "FIXA_PAREADA",
            datetime(2026, 1, 31, 23, 55, tzinfo=UTC),
            referencia("p-j1-v2", datetime(2026, 1, 31, 23, 55, tzinfo=UTC), versao, linha), 301,
        ),
    ))
    segunda_janela = ResultadoPontualidade((
        DiferencaSaida(
            "j2-v1", date(2026, 1, 31), "CALCULADA", "FIXA_PAREADA",
            datetime(2026, 2, 1, 0, 5, tzinfo=UTC),
            referencia("p-j2-v1", datetime(2026, 2, 1, 0, 5, tzinfo=UTC), versao, linha), 0,
        ),
    ))
    registro_janela_1 = publicar_pontualidade(
        primeira_janela, linha=linha.codigo, sentido="0",
        inicio=inicio_janela_1, fim=fim_janela_1, versao_gtfs_id=versao.id,
        versao_correspondencia="match-janelas-v1", versao_reconstrucao="recon-janelas-v1",
    )
    registro_janela_2 = publicar_pontualidade(
        segunda_janela, linha=linha.codigo, sentido="0",
        inicio=inicio_janela_2, fim=fim_janela_2, versao_gtfs_id=versao.id,
        versao_correspondencia="match-janelas-v1", versao_reconstrucao="recon-janelas-v1",
    )
    assert (registro_janela_1.contagem_numerador,
            registro_janela_1.contagem_denominador, registro_janela_1.valor) == (1, 2, 50.0)
    assert (registro_janela_2.contagem_numerador,
            registro_janela_2.contagem_denominador, registro_janela_2.valor) == (1, 1, 100.0)

    agregado_dia_operacional = calcular_percentual_pontualidade(
        primeira_janela.viagens + segunda_janela.viagens,
    )
    assert (agregado_dia_operacional.pontuais,
            agregado_dia_operacional.realizadas,
            agregado_dia_operacional.percentual) == (2, 3, Decimal("66.67"))
    assert {item.data_operacao for item in primeira_janela.viagens + segunda_janela.viagens} == {
        date(2026, 1, 31),
    }


def test_data_operacional_preserva_dia_e_mes_na_virada_de_calendario():
    item = executar_caso("11", 600)
    assert item.data_operacao == date(2026, 1, 31)
    assert item.saida_observada == datetime(2026, 2, 1, 0, 0, tzinfo=UTC)
    assert item.programacao.data_operacao == date(2026, 1, 31)

