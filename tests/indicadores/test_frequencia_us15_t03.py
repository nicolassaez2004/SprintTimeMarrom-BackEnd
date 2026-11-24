"""US15-T03: frequência observada e programada publicada em GET /indicadores.

Reaproveita o cenário da US15-T02 (linha 3762-10, paradas A→B→C→D, coleta de
12:00 a 13:00 UTC em 24/11/2026). Os testes de agregação gravam janelas
sintéticas em ``IndicadorOperacional`` no mesmo formato da US15-T02.
"""

from datetime import date, datetime, timedelta, timezone

import pytest

from app import db
from app.indicadores.frequencia import OBSERVADA, PROGRAMADA, UNIDADE, calcular_e_publicar_frequencias
from app.models import IndicadorOperacional, Linha
from test_frequencia_us15_t02 import DEPOIS, MATCH, RECON, T0, cenario, passagem  # noqa: F401

UTC = timezone.utc
NOV1 = datetime(2026, 11, 1, 3, tzinfo=UTC)  # 01/11 00:00 em São Paulo
COMUNS = ["linha", "sentido", "tipo", "indicador", "valor", "unidade", "inicio", "fim",
          "versao_conjunto", "versao_gtfs_id", "contagens", "qualidade"]


def consultar(app, **params):
    params = {"tipo": "FREQUENCIA", "linha": "3762-10", **params}
    resposta = app.test_client().get("/indicadores", query_string=params)
    assert resposta.status_code == 200, resposta.get_json()
    return resposta.get_json()


def janela(c, inicio, n, *, indicador=OBSERVADA, parada="B", minutos=15, data=None, motivo=None,
           sentido="0", recon=RECON, conjunto=None, linha="3762-10"):
    """Grava uma janela como a US15-T02 grava (n=None → NULL com motivo)."""
    fim = inicio + timedelta(minutes=minutos)
    data = data or (inicio - timedelta(hours=3)).date().isoformat()
    horas = minutos / 60
    db.session.add(IndicadorOperacional(
        versao_gtfs_id=c["versao"].id, linha_codigo=linha, sentido=sentido, parada_codigo=parada,
        indicador=indicador, valor=None if n is None else round(n / horas, 6), unidade=UNIDADE,
        periodo_inicio=inicio, periodo_fim=fim, contagem_numerador=n,
        versao_conjunto=conjunto or f"conj-{data}-{recon}-{sentido}", versao_correspondencia=MATCH,
        versao_calculo="frequencia-1.0",
        qualidade={"situacao": "DADOS_AUSENTES" if n is None else "CALCULADA",
                   "motivo": motivo or ("coleta_sem_dados" if n is None else None),
                   "duracao_horas": horas, "data_operacao": data, "versao_correspondencia": MATCH,
                   "versao_reconstrucao": recon, "versao_estimativa": "us15-t01-v1",
                   "coleta": {"situacoes_15min": ["COMPLETA"], "confiavel": n is not None}}))


def dia(c, inicio, contagens, **kw):
    for i, n in enumerate(contagens):
        janela(c, inicio + timedelta(minutes=15 * i), n, **kw)


def unico(corpo):
    assert len(corpo["dados"]) == 1, corpo["dados"]
    return corpo["dados"][0]


# --- ponta a ponta com a US15-T02 ----------------------------------------------------

@pytest.fixture
def publicado(cenario, app):
    passagem(cenario, "A", T0 + timedelta(minutes=1))
    passagem(cenario, "A", T0 + timedelta(minutes=2))
    passagem(cenario, "B", T0 + timedelta(minutes=5))
    passagem(cenario, "B", T0 + timedelta(minutes=50))
    calcular_e_publicar_frequencias(
        linha="3762-10", sentido="0", data_operacao=date(2026, 11, 24), versao_gtfs_id=cenario["versao"].id,
        versao_correspondencia=MATCH, versao_reconstrucao=RECON, agora=DEPOIS, intervalo=60)
    return cenario


def test_observada_e_programada_lado_a_lado(publicado, app):
    item = unico(consultar(app, sentido="ida", parada="A", inicio=T0.isoformat(),
                           fim=(T0 + timedelta(minutes=15)).isoformat()))
    assert (item["tipo"], item["unidade"], item["agregacao"]) == ("FREQUENCIA", "passagens/h", "janela")
    assert (item["observada"]["valor"], item["observada"]["contagem"]) == (8.0, 2)
    assert (item["programada"]["valor"], item["programada"]["contagem"]) == (4.0, 1)
    assert item["valor"] == 8.0 and item["contagens"] == {"numerador": 2, "denominador": None}
    assert (item["inicio"], item["fim"], item["duracao_horas"]) == (
        "2026-11-24T12:00:00+00:00", "2026-11-24T12:15:00+00:00", 0.25)
    assert item["qualidade"]["coleta"]["situacoes_15min"] == ["COMPLETA"]
    assert item["qualidade"]["versoes"]["capturas_programacao"][0]["versao_fonte"] == "captura-1"


def test_uma_passagem_em_15_minutos_vale_4_por_hora(publicado, app):
    item = unico(consultar(app, parada="B", inicio=T0.isoformat(), fim=T0.isoformat()))
    assert (item["observada"]["valor"], item["observada"]["contagem"]) == (4.0, 1)
    # Parada intermediária: sem horário programado, NULL explícito (pendência US04).
    assert item["programada"]["valor"] is None
    assert item["programada"]["motivos"] == ["programada_nao_publicada_na_parada"]


def test_faixa_horaria_soma_contagens_da_us15_t02(publicado, app):
    periodo = {"inicio": "2026-11-24T00:00:00-03:00", "fim": "2026-11-25T00:00:00-03:00",
               "agregacao": "faixa", "hora_inicio": "09:00", "hora_fim": "10:00"}
    b = unico(consultar(app, parada="B", **periodo))
    assert (b["observada"]["valor"], b["observada"]["contagem"], b["duracao_horas"]) == (2.0, 2, 1.0)
    assert b["referencia"] == "2026-11-24" and b["observada"]["cobertura"]["janelas"] == 4
    a = unico(consultar(app, parada="A", **periodo))
    assert (a["programada"]["valor"], a["programada"]["contagem"]) == (3.0, 3)
    # Fora da coleta (13:00 UTC = 10:00 local em diante): NULL, não zero.
    seguinte = unico(consultar(app, parada="B", **{**periodo, "hora_inicio": "10:00", "hora_fim": "11:00"}))
    assert seguinte["observada"]["valor"] is None
    assert seguinte["observada"]["situacao"] == "DADOS_AUSENTES"
    assert seguinte["qualidade"]["motivos_ausencia"] == ["coleta_sem_dados", "programada_nao_publicada_na_parada"]


# --- agregações com janelas sintéticas -----------------------------------------------

def test_agrega_contagens_antes_de_calcular_a_taxa(cenario, app):
    dia(cenario, NOV1, [1] * 24 + [0] * 72, data="2026-11-01")                     # 24 em 24 h → 1/h
    dia(cenario, NOV1 + timedelta(days=1), [1] * 48 + [0] * 48 + [0, 1], data="2026-11-02")  # 49 em 24,5 h → 2/h
    db.session.commit()
    periodo = {"inicio": "2026-11-01T00:00:00-03:00", "fim": "2026-11-03T00:00:00-03:00", "parada": "B"}
    dias = consultar(app, agregacao="dia", **periodo)["dados"]
    assert [(d["referencia"], d["valor"], d["observada"]["contagem"], d["duracao_horas"]) for d in dias] == [
        ("2026-11-01", 1.0, 24, 24.0), ("2026-11-02", 2.0, 49, 24.5)]
    assert dias[1]["fim"] == "2026-11-03T03:30:00+00:00"  # janelas após meia-noite ficam no dia 02
    mes = unico(consultar(app, agregacao="mes", **periodo))
    assert mes["referencia"] == "2026-11" and mes["observada"]["contagem"] == 73
    assert mes["valor"] == pytest.approx(73 / 48.5)  # não é (1 + 2) / 2 nem 1 + 2
    assert mes["observada"]["cobertura"]["duracao_esperada_horas"] == 48.0
    faixa = unico(consultar(app, agregacao="mes", hora_inicio="00:00", hora_fim="06:00", **periodo))
    assert (faixa["observada"]["contagem"], faixa["duracao_horas"]) == (24 + 24, 12.0)


def test_zero_observado_e_diferente_de_null(cenario, app):
    janela(cenario, NOV1, 0)
    janela(cenario, NOV1 + timedelta(minutes=15), None)
    db.session.commit()
    zero, nulo = consultar(app, inicio=NOV1.isoformat(), fim=(NOV1 + timedelta(minutes=30)).isoformat())["dados"]
    assert (zero["valor"], zero["observada"]["contagem"], zero["observada"]["situacao"]) == (0.0, 0, "ZERO_OBSERVADO")
    assert (nulo["valor"], nulo["observada"]["contagem"], nulo["observada"]["situacao"]) == (None, None, "DADOS_AUSENTES")
    assert nulo["observada"]["motivos"] == ["coleta_sem_dados"]


def test_cobertura_parcial_e_lacunas_tornam_o_agregado_null(cenario, app):
    dia(cenario, NOV1, [1] * 95 + [None])                       # uma janela sem coleta
    dia(cenario, NOV1 + timedelta(days=1), [1] * 50)            # 46 janelas não publicadas
    db.session.commit()
    parcial, lacuna = consultar(app, agregacao="dia", inicio="2026-11-01T00:00:00-03:00",
                                fim="2026-11-03T00:00:00-03:00")["dados"]
    assert (parcial["valor"], parcial["observada"]["situacao"]) == (None, "COBERTURA_PARCIAL")
    cobertura = parcial["observada"]["cobertura"]
    assert (cobertura["contagem_valida_parcial"], cobertura["janelas_validas"]) == (95, 95)
    assert cobertura["fracao_valida"] == pytest.approx(95 / 96)
    assert lacuna["valor"] is None and lacuna["observada"]["motivos"] == ["sem_publicacao"]
    assert lacuna["observada"]["cobertura"]["lacunas"] == [
        {"inicio": "2026-11-02T15:30:00+00:00", "fim": "2026-11-03T03:00:00+00:00"}]


def test_preserva_versoes_e_nao_mistura_conjuntos(cenario, app):
    janela(cenario, NOV1, 1)
    janela(cenario, NOV1, 3, recon="recon-2")                     # outra reconstrução: item separado
    janela(cenario, NOV1, 1, parada="C")
    janela(cenario, NOV1, 2, parada="C", conjunto="outra-captura")  # mesma versão, conjunto sobreposto
    db.session.commit()
    params = {"agregacao": "dia", "inicio": "2026-11-01T00:00:00-03:00", "fim": "2026-11-02T00:00:00-03:00"}
    b = consultar(app, parada="B", **params)["dados"]
    assert sorted(x["qualidade"]["versoes"]["versao_reconstrucao"] for x in b) == sorted([RECON, "recon-2"])
    assert all(x["versao_calculo"] == "frequencia-1.0" and x["versao_correspondencia"] == MATCH for x in b)
    c = unico(consultar(app, parada="C", **params))
    assert (c["valor"], c["observada"]["situacao"]) == (None, "CONJUNTOS_INCOMPATIVEIS")
    assert len(c["qualidade"]["versoes"]["versoes_conjunto"]) == 2


def test_filtros_combinados(cenario, app):
    db.session.add(Linha(versao_gtfs_id=cenario["versao"].id, codigo="9999-10", nome="Outra"))
    db.session.flush()
    janela(cenario, NOV1, 1)
    janela(cenario, NOV1, 2, parada="C")
    janela(cenario, NOV1, 3, sentido="1")
    janela(cenario, NOV1, 4, linha="9999-10")
    janela(cenario, NOV1 + timedelta(minutes=15), 5)
    db.session.commit()
    corpo = consultar(app, sentido="0", parada="B", inicio=NOV1.isoformat(),
                      fim=(NOV1 + timedelta(minutes=15)).isoformat())
    item = unico(corpo)
    assert (item["linha"], item["sentido"], item["parada"], item["observada"]["contagem"]) == ("3762-10", "0", "B", 1)
    assert len(consultar(app, inicio=NOV1.isoformat(), fim=(NOV1 + timedelta(hours=1)).isoformat())["dados"]) == 4


def test_limites_temporais_e_faixa_apos_meia_noite(cenario, app):
    # Janelas 23:45–00:30 locais da data operacional 01/11 (GTFS 24:xx).
    for i, n in enumerate((1, 2, 3)):
        janela(cenario, NOV1 + timedelta(hours=23, minutes=45 + 15 * i), n, data="2026-11-01")
    janela(cenario, NOV1 + timedelta(days=1), 7, data="2026-11-02")   # 00:00 do dia seguinte
    db.session.commit()
    meia_noite = NOV1 + timedelta(days=1)
    # [inicio, fim): a janela que termina no inicio e a que começa no fim ficam fora.
    corpo = consultar(app, inicio=meia_noite.isoformat(), fim=(meia_noite + timedelta(minutes=15)).isoformat())
    assert sorted(x["observada"]["contagem"] for x in corpo["dados"]) == [2, 7]
    noturna = consultar(app, agregacao="faixa", hora_inicio="23:00", hora_fim="01:00",
                        inicio="2026-11-01T00:00:00-03:00", fim="2026-11-02T00:00:00-03:00")
    item = unico(noturna)
    assert (item["referencia"], item["observada"]["contagem"]) == ("2026-11-01", None)
    assert item["observada"]["motivos"] == ["sem_publicacao"]  # 23:00–23:45 e 00:30–01:00 sem publicação
    assert item["observada"]["cobertura"]["contagem_valida_parcial"] == 6


@pytest.mark.parametrize("params,campo", [
    ({"agregacao": "dia"}, "agregacao"),                                    # sem tipo=FREQUENCIA
    ({"tipo": "FREQUENCIA", "inicio": "2026-11-01T00:00:00Z", "fim": "2026-11-02T00:00:00Z"}, "linha"),
    ({"tipo": "FREQUENCIA", "linha": "3762-10"}, "inicio"),
    ({"tipo": "FREQUENCIA", "linha": "3762-10", "agregacao": "dia",
      "inicio": "2026-11-01T00:00:00Z", "fim": "2026-11-02T00:00:00Z"}, "inicio"),   # não é meia-noite local
    ({"tipo": "FREQUENCIA", "linha": "3762-10", "agregacao": "faixa",
      "inicio": "2026-11-01T00:00:00-03:00", "fim": "2026-11-02T00:00:00-03:00"}, "hora_inicio"),
    ({"tipo": "FREQUENCIA", "linha": "3762-10", "agregacao": "semana",
      "inicio": "2026-11-01T00:00:00-03:00", "fim": "2026-11-02T00:00:00-03:00"}, "agregacao"),
    ({"tipo": "FREQUENCIA", "linha": "3762-10", "inicio": "2026-10-01T00:00:00Z",
      "fim": "2026-11-02T00:00:00Z"}, "inicio"),                                      # > 31 dias
])
def test_parametros_invalidos(app, params, campo):
    resposta = app.test_client().get("/indicadores", query_string=params)
    assert resposta.status_code == 400
    assert resposta.get_json()["erro"]["campo"] == campo


def test_compatibilidade_com_os_demais_indicadores(cenario, app):
    janela(cenario, NOV1, 1)
    janela(cenario, NOV1, 1, indicador=PROGRAMADA, parada="A")
    db.session.add(IndicadorOperacional(
        versao_gtfs_id=cenario["versao"].id, linha_codigo="3762-10", sentido="0",
        indicador="CUMPRIMENTO_VIAGENS", valor=87.5, unidade="%", periodo_inicio=NOV1,
        periodo_fim=NOV1 + timedelta(days=1), contagem_numerador=7, contagem_denominador=8,
        versao_conjunto="cump", qualidade={}))
    db.session.commit()
    cliente = app.test_client()
    todos = cliente.get("/indicadores?linha=3762-10").get_json()
    assert sorted(x["tipo"] for x in todos["dados"]) == ["CUMPRIMENTO_VIAGENS", OBSERVADA, PROGRAMADA]
    assert all("observada" not in x for x in todos["dados"])  # formato comum inalterado
    bruto = cliente.get(f"/indicadores?tipo={OBSERVADA}").get_json()["dados"]
    assert bruto[0]["valor"] == 4.0 and bruto[0]["contagens"] == {"numerador": 1, "denominador": None}
    item = consultar(app, inicio=NOV1.isoformat(), fim=(NOV1 + timedelta(minutes=15)).isoformat())["dados"][0]
    assert all(campo in item for campo in COMUNS)
    series = cliente.get("/series?linha=3762-10&tipo=CUMPRIMENTO_VIAGENS")
    assert series.status_code == 200 and series.get_json()["dados"][0]["valor"] == 87.5
