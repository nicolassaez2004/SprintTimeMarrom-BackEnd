"""US26-T03: limiar, ausência total e atualização da qualidade publicada (dados SINTÉTICOS).

Ponta a ponta pela agenda da US23-T03 (``atualizar_indicadores`` → ``gravar_indicador`` →
``FalhasColeta`` da US26-T01) e lido pelas rotas públicas e pelos CSV gerados no servidor.
Linha 3762-10, janela exibida 12:00–12:15 UTC de 24/11/2026, intervalo de 30 s (30 ciclos).
SQLite em memória e diretório temporário: nada toca o MySQL compartilhado.
"""

import csv
import io
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from app import db
from app.exportacoes import csv as exportador
from app.indicadores.agenda import atualizar_indicadores, consolidar_dia
from app.models import ExecucaoColeta, IndicadorOperacional, LacunaColeta, RegistroBrutoGPS, ViagemObservada
from app.qualidade.service import VERSAO_REGRA_FALHAS
from test_frequencia_us15_t03 import T0 as T0_FREQ, cenario, publicado  # noqa: F401

UTC = timezone.utc
T0 = datetime(2026, 11, 24, 12, tzinfo=UTC)
DIA = date(2026, 11, 24)
JANELA = {"inicio": "2026-11-24T12:00:00Z", "fim": "2026-11-24T12:15:00Z"}
LINHA = {"linha": "3762-10", "sentido": "ida"}
CONSOLIDACAO = datetime(2026, 11, 25, 7, 1, tzinfo=UTC)  # 04:01 em São Paulo


@pytest.fixture
def painel(app, programacao, tmp_path):
    app.config.update(GPS_COLLECTION_INTERVAL_SECONDS=30, EXPORTACOES_DIR=str(tmp_path / "exportacoes"))
    _, _, _, partidas = programacao
    db.session.add(ViagemObservada(
        partida_programada=partidas[0], saida_observada=partidas[0].saida_prevista + timedelta(seconds=30),
        cobertura_tracado=0.9, versao_reconstrucao="recon-1", versao_correspondencia="match-1"))
    db.session.commit()
    return app.test_client()


def ciclos(indices, estado="OK", passo=30.0, erro=None):
    for k in indices:
        instante = T0 + timedelta(seconds=passo * k)
        db.session.add(ExecucaoColeta(prevista_em=instante, iniciada_em=instante, estado=estado, erro=erro))
    db.session.commit()


# Cada cenário grava os ciclos de coleta (frota inteira) que a janela 12:00–12:15 viu.
CENARIOS = {
    "zero": lambda: ciclos(range(30)),
    "dez": lambda: ciclos(k for k in range(30) if k not in (10, 11, 12)),            # 3/30
    # 10,1% exige denominador 1000: 1000 ciclos registrados (> 30 esperados) e 101 sem dados.
    "dez_virgula_um": lambda: (ciclos(range(899), passo=0.9),
                               ciclos(range(899, 1000), estado="FALHA", passo=0.9, erro="HTTP 503")),
    "ausencia_total": lambda: None,
}
ESPERADO = {  # percentual, incompleto, situacao, motivo
    "zero": (0.0, False, "COMPLETA", None),
    "dez": (10.0, False, "COM_FALHAS", None),
    "dez_virgula_um": (10.1, True, "INCOMPLETA", "FALHAS_ACIMA_DO_LIMITE"),
    "ausencia_total": (None, None, "SEM_EXPECTATIVA", "FORA_DO_MONITORAMENTO"),
}


def publicar(cliente):
    atualizar_indicadores(T0 + timedelta(minutes=16))  # publica a janela 12:00–12:15
    return cliente.get("/series", query_string=LINHA).get_json()


def resumo(bloco):
    return bloco["percentual_falhas"], bloco["incompleto"], bloco["situacao"], bloco["motivo"]


def get_json(cliente, url, **params):
    resposta = cliente.get(url, query_string=params)
    assert resposta.status_code == 200, resposta.get_json()
    return resposta.get_json()


def linhas_csv(resposta):
    assert resposta.status_code == 200, resposta.get_json()
    return list(csv.DictReader(io.StringIO(resposta.data.decode("utf-8-sig"))))


def exportar(cliente, versao):
    return cliente.get("/downloads/indicadores", query_string={
        "formato": "csv", "data": DIA.isoformat(), **LINHA, "versao_dados": versao})


def da_janela(itens, chave="inicio"):
    return [i for i in itens if i[chave] == "2026-11-24T12:00:00+00:00"]


# --- os quatro cenários: API (/indicadores, /series, /qualidade) e CSV -----------------

@pytest.mark.parametrize("cenario", list(CENARIOS))
def test_api_e_csv_publicam_a_mesma_qualidade(painel, cenario):
    CENARIOS[cenario]()
    serie = publicar(painel)

    pontos = da_janela(serie["dados"])
    assert {p["tipo"] for p in pontos} == {"CUMPRIMENTO_VIAGENS", "PONTUALIDADE_VIAGENS"}
    blocos = [p["qualidade"]["falhas_coleta"] for p in pontos]
    assert all(b == blocos[0] for b in blocos)  # mesma janela, mesmo bloco em todos os indicadores
    bloco = blocos[0]
    assert resumo(bloco) == ESPERADO[cenario]
    # Escopo real da medição: ciclos da frota inteira, apenas exibidos na linha.
    assert (bloco["escopo"], bloco["linha_exibida"], bloco["versao_regra"]) == (
        "COLETA_GLOBAL", "3762-10", VERSAO_REGRA_FALHAS)
    assert bloco["limite_percentual"] == 10.0

    indicadores = get_json(painel, "/indicadores", tipo="CUMPRIMENTO_VIAGENS", **LINHA, **JANELA)
    assert [i["qualidade"]["falhas_coleta"] for i in indicadores["dados"]] == [bloco]

    # Mesmo número que a US13 publica para a janela de 15 min (sem recontagem).
    relatorio = get_json(painel, "/qualidade", inicio=DIA.isoformat(), janelas="true")
    janela = da_janela(relatorio["dias"][0]["janelas"])[0]
    assert (janela["percentual_falhas"], janela["incompleto"]) == (bloco["percentual_falhas"], bloco["incompleto"])
    assert janela["ciclos_com_dados"] == bloco["ciclos_com_dados"]

    # CSV da versão exibida: percentual, contagens, motivo e unidade idênticos à API.
    linhas = da_janela(linhas_csv(exportar(painel, serie["versao_dados"]["id"])))
    assert {r["tipo"] for r in linhas} == {"CUMPRIMENTO_VIAGENS", "PONTUALIDADE_VIAGENS"}
    for r in linhas:
        assert json.loads(r["qualidade"])["falhas_coleta"] == bloco
        assert r["unidade"] == "%"


def test_ausencia_total_nao_vira_zero_falhas(painel):
    serie = publicar(painel)
    bloco = da_janela(serie["dados"])[0]["qualidade"]["falhas_coleta"]
    assert bloco["percentual_falhas"] is None and bloco["incompleto"] is None
    assert (bloco["ciclos_esperados"], bloco["denominador"], bloco["falhas"]) == (0, 0, 0)
    assert bloco["operacao_linha"] is None  # sem coleta, nada se afirma sobre a linha
    linha = da_janela(linhas_csv(exportar(painel, serie["versao_dados"]["id"])))[0]
    falhas = json.loads(linha["qualidade"])["falhas_coleta"]
    assert falhas["percentual_falhas"] is None and falhas["incompleto"] is None  # null, não 0 nem ""
    assert '"percentual_falhas":null' in linha["qualidade"]


def test_coleta_ativa_sem_nenhum_ciclo_e_lacuna_aberta(painel):
    ciclos([-1])  # monitoramento começou 11:59:30; a janela inteira em silêncio
    lacuna = LacunaColeta(id="lacuna-us26-t03", inicio=T0, fim=None, causa="SPTransTimeoutError",
                          fonte="OLHO_VIVO", aberta_fonte="OLHO_VIVO")
    db.session.add(lacuna)
    db.session.commit()
    serie = publicar(painel)
    bloco = da_janela(serie["dados"])[0]["qualidade"]["falhas_coleta"]
    assert resumo(bloco) == (100.0, True, "SEM_DADOS", "NENHUM_CICLO_COM_DADOS")
    assert (bloco["denominador"], bloco["ciclos_com_dados"]) == (30, 0)
    assert [(l["id"], l["estado"], l["fim"]) for l in bloco["lacunas"]] == [("lacuna-us26-t03", "ABERTA", None)]
    linha = da_janela(linhas_csv(exportar(painel, serie["versao_dados"]["id"])))[0]
    assert json.loads(linha["qualidade"])["falhas_coleta"]["lacunas"] == bloco["lacunas"]


def test_ausencia_operacional_nao_e_falha_de_coleta(painel):
    ciclos(range(30))  # coleta válida, mas nenhum bruto da linha
    bloco = da_janela(publicar(painel)["dados"])[0]["qualidade"]["falhas_coleta"]
    assert resumo(bloco) == (0.0, False, "COMPLETA", None)
    assert bloco["operacao_linha"] == "AUSENCIA_OPERACIONAL"


def test_falha_efetiva_de_coleta_nao_e_ausencia_operacional(painel):
    ciclos(range(30), estado="FALHA", erro="SPTransHTTPError: 503")
    bloco = da_janela(publicar(painel)["dados"])[0]["qualidade"]["falhas_coleta"]
    assert resumo(bloco) == (100.0, True, "SEM_DADOS", "NENHUM_CICLO_COM_DADOS")
    assert (bloco["ciclos_falha"], bloco["falhas_da_fonte"]) == (30, 30)
    assert bloco["operacao_linha"] is None


# --- atualização após nova coleta / recálculo, preservando a versão publicada ----------

def test_recalculo_atualiza_percentual_e_motivo_sem_alterar_versao_anterior(painel):
    ciclos(range(26))
    ciclos(range(26, 30), estado="EM_ANDAMENTO")  # 4/30 ainda sem resultado na publicação
    antes = publicar(painel)
    v1 = antes["versao_dados"]["id"]
    bloco_v1 = da_janela(antes["dados"])[0]["qualidade"]["falhas_coleta"]
    assert resumo(bloco_v1) == (13.33, True, "INCOMPLETA", "FALHAS_ACIMA_DO_LIMITE")
    csv_v1 = exportar(painel, v1).data
    brutos = RegistroBrutoGPS.query.count()

    # Nova coleta: um dos ciclos termina (retomada); a consolidação recalcula o dia.
    ExecucaoColeta.query.filter_by(prevista_em=T0 + timedelta(seconds=30 * 26)).one().estado = "OK"
    db.session.commit()
    consolidar_dia(DIA, CONSOLIDACAO)
    exportador.publicar_csv_indicadores(DIA, CONSOLIDACAO)

    depois = get_json(painel, "/series", **LINHA, **JANELA)
    assert depois["versao_dados"]["id"] != v1
    for ponto in da_janela(depois["dados"]):
        assert resumo(ponto["qualidade"]["falhas_coleta"]) == (10.0, False, "COM_FALHAS", None)
    atual = get_json(painel, "/indicadores", tipo="CUMPRIMENTO_VIAGENS", **LINHA, **JANELA)["dados"]
    assert [resumo(i["qualidade"]["falhas_coleta"]) for i in atual] == [(10.0, False, "COM_FALHAS", None)]

    # Exportações seguintes: versão nova e arquivo diário consolidado refletem o recálculo.
    novo = da_janela(linhas_csv(exportar(painel, depois["versao_dados"]["id"])))
    diario = da_janela(linhas_csv(painel.get("/downloads/indicadores", query_string={
        "formato": "csv", "data": DIA.isoformat(), **LINHA})))
    for r in novo + diario:
        assert resumo(json.loads(r["qualidade"])["falhas_coleta"]) == (10.0, False, "COM_FALHAS", None)

    # Histórico: a versão exibida antes continua idêntica, byte a byte, com o aviso antigo.
    antigo = exportar(painel, v1)
    assert antigo.data == csv_v1 and antigo.headers["X-Versao-Dados"] == v1
    assert {json.loads(r["qualidade"])["falhas_coleta"]["percentual_falhas"]
            for r in da_janela(linhas_csv(antigo))} == {13.33}
    assert RegistroBrutoGPS.query.count() == brutos  # recálculo não toca os brutos
    assert IndicadorOperacional.query.filter_by(periodo_inicio=T0).count() == 2  # upsert, sem duplicar


# --- consultas compostas (FREQUENCIA/REGULARIDADE/ESPERA) e o intervalo do cálculo -------

def test_frequencia_por_janela_publica_o_bloco_gravado(publicado, app):
    gravados = {(r.indicador, r.parada_codigo): r.qualidade for r in
                IndicadorOperacional.query.filter_by(periodo_inicio=T0_FREQ)}
    corpo = get_json(app.test_client(), "/indicadores", tipo="FREQUENCIA", linha="3762-10",
                     inicio="2026-11-24T12:00:00Z", fim="2026-11-24T12:15:00Z")
    itens = [i for i in corpo["dados"] if i["inicio"] == T0_FREQ.isoformat()]
    assert itens
    for item in itens:
        bloco = item["qualidade"]["falhas_coleta"]
        assert bloco == gravados[("FREQUENCIA_OBSERVADA", item["parada"])]["falhas_coleta"]
        # Mesmo intervalo (60 s) da US13 usada pelo cálculo: as duas qualidades concordam.
        assert bloco["intervalo_coleta_segundos"] == 60
        assert resumo(bloco)[:3] == (0.0, False, "COMPLETA")
        assert gravados[("FREQUENCIA_OBSERVADA", item["parada"])]["coleta"]["situacoes_15min"] == ["COMPLETA"]
    # Agregações dia/mês/faixa exibem outra janela: o bloco da janela de 15 min não é repassado.
    dia = get_json(app.test_client(), "/indicadores", tipo="FREQUENCIA", linha="3762-10", agregacao="dia",
                   inicio="2026-11-24T03:00:00Z", fim="2026-11-25T03:00:00Z")
    assert dia["dados"] and all("falhas_coleta" not in i["qualidade"] for i in dia["dados"])
