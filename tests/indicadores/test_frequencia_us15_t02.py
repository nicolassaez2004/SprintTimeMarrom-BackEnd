"""US15-T02: frequência observada e programada por janela (dados sintéticos).

Fixture ``programacao``: linha 3762-10, sentido 0, data operacional 24/11/2026,
partidas às 12:00, 12:15 e 12:30 UTC. Percurso A → B → C → D. Coleta (US13)
com ciclos de 60 s de 12:00 a 13:00 UTC: antes não havia monitoramento e
depois houve silêncio, ou seja, janelas sem dados.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app import db
from app.models import (
    EstimativaPassagemViagem, ExecucaoColeta, ImportacaoProgramacao, IndicadorOperacional, Parada, PartidaProgramada,
    PassagemEstimada, RegistroBrutoGPS, Trecho, ViagemObservada,
)
from app.indicadores.frequencia import (
    OBSERVADA, PROGRAMADA, UNIDADE, calcular_e_publicar_frequencias, calcular_frequencias,
)
from app.viagens.passagens import versao_estimativa

T0 = datetime(2026, 11, 24, 12, tzinfo=timezone.utc)
DEPOIS = datetime(2026, 11, 26, 12, tzinfo=timezone.utc)
PARADAS = {"A": -46.700, "B": -46.697, "C": -46.694, "D": -46.690}
RECON, MATCH = "recon-sint", "match-sint"
LACUNA_SEG = 120  # valor de teste; o oficial aguarda o PO (US15-T04 P01)
VERSAO = versao_estimativa(timedelta(seconds=LACUNA_SEG))


@pytest.fixture(autouse=True)
def lacuna_de_teste(app):
    app.config["PASSAGEM_LACUNA_MAXIMA_SEG"] = LACUNA_SEG


def estimada(viagem, sem_evidencia=()):
    """Registra que a estimativa da US15-T01 rodou para a viagem."""
    db.session.add(EstimativaPassagemViagem(
        viagem_observada_id=viagem.id, versao_estimativa=VERSAO, lacuna_maxima_seg=LACUNA_SEG,
        executada_em=DEPOIS, sem_evidencia=list(sem_evidencia)))


@pytest.fixture
def cenario(programacao):
    versao, linha, importacao, partidas = programacao
    db.session.add_all(Parada(versao_gtfs_id=versao.id, codigo=c, nome=c, latitude=Decimal("-23.55"),
                              longitude=Decimal(str(lon))) for c, lon in PARADAS.items())
    codigos = list(PARADAS)
    db.session.add_all(
        Trecho(versao_gtfs_id=versao.id, linha_codigo=linha.codigo, sentido="0", trajeto="T1",
               ordem=i + 1, parada_origem_codigo=o, parada_destino_codigo=d,
               geometria=[[PARADAS[o], -23.55], [PARADAS[d], -23.55]])
        for i, (o, d) in enumerate(zip(codigos, codigos[1:])))
    execucoes = [ExecucaoColeta(prevista_em=T0 + timedelta(minutes=m), iniciada_em=T0 + timedelta(minutes=m),
                                estado="OK", recebidos=1) for m in range(60)]
    # Captura GTFS do mesmo dia só para hospedar viagens sintéticas (uma por
    # partida e versão); a US04 prefere a OFICIAL, então a programada não muda.
    apoio = ImportacaoProgramacao(
        versao_gtfs_id=versao.id, linha_codigo=linha.codigo, data_operacao=importacao.data_operacao,
        sentido="0", tipo_dia="UTIL", fonte="GTFS", versao_fonte="apoio", fonte_url="gtfs://apoio",
        capturada_em=T0, verificada_em=T0, arquivo=importacao.arquivo)
    db.session.add_all([*execucoes, apoio])
    db.session.commit()
    return {"versao": versao, "linha": linha, "importacao": importacao, "partidas": partidas,
            "execucao": execucoes[0], "apoio": apoio, "seq": [0]}


def passagem(c, parada, instante, partida=None, recon=RECON):
    """Grava uma PassagemEstimada com sua viagem e par de posições brutas."""
    c["seq"][0] += 1
    brutos = [RegistroBrutoGPS(execucao=c["execucao"], prefixo_origem=f"V{c['seq'][0]}",
                               latitude=-23.55, longitude=-46.7, horario_gps=instante + timedelta(seconds=s))
              for s in (-10, 10)]
    if partida is None:
        partida = PartidaProgramada(importacao=c["apoio"], ordem=c["seq"][0], saida_prevista=instante)
    viagem = ViagemObservada(partida_programada=partida, saida_observada=instante,
                             cobertura_tracado=1, versao_reconstrucao=recon, versao_correspondencia=MATCH)
    db.session.add_all([*brutos, viagem])
    db.session.flush()
    db.session.add(PassagemEstimada(viagem=viagem, versao_gtfs_id=c["versao"].id, parada_codigo=parada,
                                    instante=instante, metodo="INTERPOLACAO_LINEAR_PERCURSO",
                                    versao_estimativa=VERSAO, posicao_anterior_id=brutos[0].id,
                                    posicao_posterior_id=brutos[1].id))
    estimada(viagem)
    db.session.commit()
    return viagem


def calcular(c, **extra):
    parametros = dict(linha=c["linha"].codigo, sentido="0", data_operacao=date(2026, 11, 24),
                      versao_gtfs_id=c["versao"].id, versao_correspondencia=MATCH,
                      versao_reconstrucao=RECON, agora=DEPOIS, intervalo=60)
    return calcular_frequencias(**{**parametros, **extra})


def achar(campos, indicador, parada, inicio):
    encontrados = [x for x in campos if (x["indicador"], x["parada_codigo"], x["periodo_inicio"])
                   == (indicador, parada, inicio)]
    assert len(encontrados) == 1
    return encontrados[0]


def test_uma_passagem_em_15_minutos_vale_4_por_hora(cenario):
    passagem(cenario, "B", T0 + timedelta(minutes=5))
    item = achar(calcular(cenario), OBSERVADA, "B", T0)
    assert (item["valor"], item["contagem_numerador"], item["unidade"]) == (4.0, 1, UNIDADE)
    assert item["periodo_fim"] - item["periodo_inicio"] == timedelta(minutes=15)
    assert item["qualidade"]["situacao"] == "CALCULADA"
    assert item["qualidade"]["coleta"]["situacoes_15min"] == ["COMPLETA"]


def test_zero_passagens_com_coleta_suficiente_vale_zero(cenario):
    item = achar(calcular(cenario), OBSERVADA, "B", T0 + timedelta(minutes=15))
    assert (item["valor"], item["contagem_numerador"]) == (0.0, 0)
    assert item["qualidade"]["situacao"] == "ZERO_OBSERVADO"
    assert item["qualidade"]["motivo"] is None


def test_sem_coleta_e_null_com_motivo(cenario):
    campos = calcular(cenario)
    silencio = achar(campos, OBSERVADA, "B", T0 + timedelta(hours=1))
    assert silencio["valor"] is None and silencio["contagem_numerador"] is None
    assert silencio["qualidade"]["situacao"] == "DADOS_AUSENTES"
    assert silencio["qualidade"]["motivo"] == "coleta_sem_dados"
    antes = achar(campos, OBSERVADA, "B", T0 - timedelta(hours=6))
    assert antes["valor"] is None
    assert antes["qualidade"]["motivo"] == "coleta_fora_do_monitoramento"


def test_passagem_em_janela_sem_coleta_confiavel_nao_vira_valor(cenario):
    passagem(cenario, "B", T0 + timedelta(hours=2))
    item = achar(calcular(cenario), OBSERVADA, "B", T0 + timedelta(hours=2))
    assert item["valor"] is None
    assert item["qualidade"]["passagens_identificadas"] == 1


def test_conta_por_linha_sentido_e_parada(cenario):
    importacao = cenario["importacao"]
    volta = ImportacaoProgramacao(
        versao_gtfs_id=importacao.versao_gtfs_id, linha_codigo=importacao.linha_codigo,
        data_operacao=importacao.data_operacao, sentido="1", tipo_dia="UTIL", fonte="OFICIAL",
        versao_fonte="captura-volta", fonte_url="https://example.invalid/volta",
        capturada_em=T0, verificada_em=T0, arquivo=importacao.arquivo)
    partida_volta = PartidaProgramada(importacao=volta, ordem=1, saida_prevista=T0)
    db.session.add_all([volta, partida_volta])
    passagem(cenario, "B", T0 + timedelta(minutes=1))
    passagem(cenario, "B", T0 + timedelta(minutes=2), partida=cenario["partidas"][1])
    passagem(cenario, "C", T0 + timedelta(minutes=3))
    passagem(cenario, "B", T0 + timedelta(minutes=4), partida=partida_volta)  # outro sentido
    campos = calcular(cenario)
    assert achar(campos, OBSERVADA, "B", T0)["contagem_numerador"] == 2
    assert achar(campos, OBSERVADA, "C", T0)["contagem_numerador"] == 1
    assert achar(campos, OBSERVADA, "D", T0)["contagem_numerador"] == 0
    assert {x["sentido"] for x in campos} == {"0"}
    assert {x["linha_codigo"] for x in campos} == {"3762-10"}


def test_limite_da_janela_sem_dupla_contagem(cenario):
    passagem(cenario, "B", T0 + timedelta(minutes=15))
    campos = calcular(cenario)
    assert achar(campos, OBSERVADA, "B", T0)["contagem_numerador"] == 0
    assert achar(campos, OBSERVADA, "B", T0 + timedelta(minutes=15))["contagem_numerador"] == 1
    assert sum(x["contagem_numerador"] or 0 for x in campos
               if x["indicador"] == OBSERVADA and x["parada_codigo"] == "B") == 1


def test_mesma_evidencia_gps_conta_uma_vez(cenario):
    viagem = passagem(cenario, "B", T0 + timedelta(minutes=5))
    original = PassagemEstimada.query.filter_by(viagem_observada_id=viagem.id).one()
    outra = ViagemObservada(partida_programada=cenario["partidas"][1], saida_observada=T0,
                            cobertura_tracado=1, versao_reconstrucao=RECON, versao_correspondencia=MATCH)
    db.session.add(outra)
    db.session.flush()
    db.session.add(PassagemEstimada(viagem=outra, versao_gtfs_id=original.versao_gtfs_id, parada_codigo="B",
                                    instante=original.instante, metodo=original.metodo,
                                    versao_estimativa=VERSAO, posicao_anterior_id=original.posicao_anterior_id,
                                    posicao_posterior_id=original.posicao_posterior_id))
    estimada(outra)
    db.session.commit()
    assert achar(calcular(cenario), OBSERVADA, "B", T0)["contagem_numerador"] == 1


def test_programada_e_observada_separadas(cenario):
    passagem(cenario, "A", T0 + timedelta(minutes=1))
    passagem(cenario, "A", T0 + timedelta(minutes=2))
    campos = calcular(cenario)
    programada = achar(campos, PROGRAMADA, "A", T0)
    observada = achar(campos, OBSERVADA, "A", T0)
    assert (programada["valor"], programada["contagem_numerador"]) == (4.0, 1)
    assert (observada["valor"], observada["contagem_numerador"]) == (8.0, 2)
    assert programada["chave_resultado"] != observada["chave_resultado"]
    assert programada["qualidade"]["captura_programacao"]["versao_fonte"] == "captura-1"
    # Saída na origem não é passagem prevista nas paradas intermediárias.
    assert {x["parada_codigo"] for x in campos if x["indicador"] == PROGRAMADA} == {"A"}
    # Programada não depende da coleta: zero programado mesmo sem dados GPS.
    noite = achar(campos, PROGRAMADA, "A", T0 + timedelta(hours=5))
    assert (noite["valor"], noite["qualidade"]["situacao"]) == (0.0, "ZERO_PROGRAMADO")


def test_programacao_ausente_e_null(cenario):
    campos = calcular(cenario, data_operacao=date(2026, 11, 23))
    item = achar(campos, PROGRAMADA, "A", datetime(2026, 11, 23, 15, tzinfo=timezone.utc))
    assert item["valor"] is None
    assert item["qualidade"]["motivo"] == "programacao_ausente"


def test_horario_apos_meia_noite_fica_na_data_operacional(cenario):
    # GTFS 24:40:00 do dia 24 = 25/11 00:40 em São Paulo = 03:40 UTC.
    madrugada = datetime(2026, 11, 25, 3, 40, tzinfo=timezone.utc)
    db.session.add(PartidaProgramada(importacao=cenario["importacao"], ordem=4, saida_prevista=madrugada))
    db.session.commit()
    campos = calcular(cenario)
    item = achar(campos, PROGRAMADA, "A", datetime(2026, 11, 25, 3, 30, tzinfo=timezone.utc))
    assert item["contagem_numerador"] == 1
    assert item["qualidade"]["data_operacao"] == "2026-11-24"
    inicios = sorted({x["periodo_inicio"] for x in campos})
    assert inicios[0] == datetime(2026, 11, 24, 3, tzinfo=timezone.utc)  # meia-noite local
    assert inicios[-1] == datetime(2026, 11, 25, 3, 30, tzinfo=timezone.utc)
    # A data operacional seguinte não herda a partida do dia 24.
    seguinte = calcular(cenario, data_operacao=date(2026, 11, 25))
    assert all(x["qualidade"]["data_operacao"] == "2026-11-25" for x in seguinte)
    assert achar(seguinte, PROGRAMADA, "A", datetime(2026, 11, 25, 3, 30, tzinfo=timezone.utc))["valor"] is None


def test_preserva_versoes_qualidade_e_e_idempotente(cenario):
    passagem(cenario, "B", T0 + timedelta(minutes=5))
    passagem(cenario, "B", T0 + timedelta(minutes=6), recon="outra-reconstrucao")
    item = achar(calcular(cenario), OBSERVADA, "B", T0)
    assert item["contagem_numerador"] == 1
    assert (item["versao_gtfs_id"], item["versao_correspondencia"], item["versao_calculo"]) == (
        cenario["versao"].id, MATCH, "frequencia-1.0")
    assert item["qualidade"]["versao_reconstrucao"] == RECON
    assert item["qualidade"]["versao_estimativa"] == VERSAO

    publicados = calcular_e_publicar_frequencias(
        linha="3762-10", sentido="0", data_operacao=date(2026, 11, 24), versao_gtfs_id=cenario["versao"].id,
        versao_correspondencia=MATCH, versao_reconstrucao=RECON, agora=DEPOIS, intervalo=60)
    total = IndicadorOperacional.query.count()
    assert total == len(publicados) == 96 * 5
    calcular_e_publicar_frequencias(
        linha="3762-10", sentido="0", data_operacao=date(2026, 11, 24), versao_gtfs_id=cenario["versao"].id,
        versao_correspondencia=MATCH, versao_reconstrucao=RECON, agora=DEPOIS, intervalo=60)
    assert IndicadorOperacional.query.count() == total
    gravado = IndicadorOperacional.query.filter_by(chave_resultado=item["chave_resultado"]).one()
    assert (gravado.valor, gravado.qualidade["coleta"]["confiavel"]) == (4.0, True)
    assert len({r.versao_conjunto for r in IndicadorOperacional.query}) == 1


def test_menos_de_tres_passagens_continua_calculavel(cenario):
    passagem(cenario, "C", T0 + timedelta(minutes=20))
    passagem(cenario, "C", T0 + timedelta(minutes=25))
    item = achar(calcular(cenario), OBSERVADA, "C", T0 + timedelta(minutes=15))
    assert (item["valor"], item["contagem_numerador"]) == (8.0, 2)
    assert "US17" in item["qualidade"]["minimo_passagens"]


def test_agregacao_horaria_exige_todas_as_janelas_confiaveis(cenario):
    passagem(cenario, "B", T0 + timedelta(minutes=5))
    passagem(cenario, "B", T0 + timedelta(minutes=50))
    campos = calcular(cenario, janela_minutos=60)
    hora = achar(campos, OBSERVADA, "B", T0)
    assert (hora["valor"], hora["contagem_numerador"]) == (2.0, 2)
    assert hora["qualidade"]["coleta"]["situacoes_15min"] == ["COMPLETA"] * 4
    assert achar(campos, OBSERVADA, "B", T0 + timedelta(hours=1))["valor"] is None
    with pytest.raises(ValueError):
        calcular(cenario, janela_minutos=20)


def test_passagem_antes_da_meia_noite_de_partida_da_data_e_contada(cenario):
    meia_noite = datetime(2026, 11, 24, 3, tzinfo=timezone.utc)  # 24/11 00:00 em São Paulo
    passagem(cenario, "B", meia_noite - timedelta(seconds=30))
    campos = calcular(cenario)
    item = achar(campos, OBSERVADA, "B", meia_noite - timedelta(minutes=15))
    assert item["qualidade"]["passagens_identificadas"] == 1
    assert sum(x["qualidade"]["passagens_identificadas"] for x in campos
               if x["indicador"] == OBSERVADA and x["parada_codigo"] == "B") == 1


def test_zero_so_com_evidencia_gps_e_estimativa_executada(cenario, app):
    viagem = ViagemObservada(partida_programada=cenario["partidas"][0], saida_observada=T0,
                             fim_observado=T0 + timedelta(minutes=40), cobertura_tracado=1,
                             versao_reconstrucao=RECON, versao_correspondencia=MATCH)
    db.session.add(viagem)
    db.session.commit()
    # Viagem sem estimativa executada: nenhuma janela da viagem afirma zero.
    item = achar(calcular(cenario), OBSERVADA, "C", T0 + timedelta(minutes=15))
    assert item["valor"] is None
    assert item["qualidade"]["motivo"] == "estimativa_nao_executada"

    # Estimativa executada; C sem evidência entre 12:20 e 12:24 (par rejeitado).
    estimada(viagem, [{"parada": "C", "inicio": (T0 + timedelta(minutes=20)).isoformat(),
                       "fim": (T0 + timedelta(minutes=24)).isoformat()}])
    db.session.commit()
    campos = calcular(cenario)
    sem_prova = achar(campos, OBSERVADA, "C", T0 + timedelta(minutes=15))
    assert (sem_prova["valor"], sem_prova["qualidade"]["motivo"]) == (None, "sem_evidencia_gps")
    assert achar(campos, OBSERVADA, "C", T0 + timedelta(minutes=30))["valor"] == 0.0
    assert achar(campos, OBSERVADA, "B", T0 + timedelta(minutes=15))["valor"] == 0.0

    # Sem lacuna máxima configurada não há passagens: nada de zero falso.
    app.config["PASSAGEM_LACUNA_MAXIMA_SEG"] = None
    item = achar(calcular(cenario), OBSERVADA, "B", T0 + timedelta(minutes=15))
    assert (item["valor"], item["qualidade"]["motivo"]) == (None, "lacuna_maxima_indefinida")
