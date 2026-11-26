"""US19-T03 (e persistência da T01): ranking mensal dos trechos críticos. Dados sintéticos.

Fluxo A: passagens → atrasos por parada (T01) → variação persistida → agregado
mensal → GET /atrasos/ranking.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app import db
from app.core.tempo import FUSO
from app.indicadores.propagacao import (
    INDICADOR_MENSAL, INDICADOR_VARIACAO, MetricaMensalIndefinida, PropagacaoViagem, VariacaoTrecho,
    agregar_mes, calcular_propagacao, limites_mes, publicar_propagacao,
)
from app.models import IndicadorOperacional, Parada, PassagemEstimada, Trecho, ViagemObservada

NOV = date(2026, 11, 24)


@pytest.fixture
def rede(programacao):
    """25 trechos t01..t25 de um trajeto da linha 3762-10, sentido 0."""
    versao, linha, importacao, partidas = programacao
    db.session.add_all(Parada(versao_gtfs_id=versao.id, codigo=f"P{i:02d}", nome=f"P{i:02d}",
                              latitude=Decimal("-23.55"), longitude=Decimal(str(-46.7 + i / 1000)))
                       for i in range(26))
    trechos = [Trecho(id=f"t{i:02d}", versao_gtfs_id=versao.id, linha_codigo=linha.codigo, sentido="0",
                      trajeto="T1", ordem=i, parada_origem_codigo=f"P{i - 1:02d}",
                      parada_destino_codigo=f"P{i:02d}",
                      geometria=[[-46.7 + (i - 1) / 1000, -23.55], [-46.7 + i / 1000, -23.55]])
               for i in range(1, 26)]
    db.session.add_all(trechos)
    db.session.commit()
    return {"versao": versao, "importacao": importacao, "trechos": {t.id: t for t in trechos}}


def propagar(rede, viagem_id, variacoes, data=NOV, **versoes):
    """Publica uma viagem sintética: ``{trecho_id: variação (s) | None}``."""
    trechos = tuple(VariacaoTrecho(
        trecho_id=tid, ordem=rede["trechos"][tid].ordem, parada_origem=rede["trechos"][tid].parada_origem_codigo,
        parada_destino=rede["trechos"][tid].parada_destino_codigo,
        atraso_origem_seg=None if v is None else 0.0, atraso_destino_seg=v, variacao_seg=v,
        motivo_destino=None if v is not None else "passagem_ausente") for tid, v in variacoes.items())
    campos = dict(viagem_observada_id=viagem_id, partida_programada_id=f"p-{viagem_id}",
                  importacao_id=rede["importacao"].id, versao_gtfs_id=rede["versao"].id, data_operacao=data,
                  versao_reconstrucao="recon", versao_correspondencia="match", versao_estimativa="est",
                  trechos=trechos)
    return publicar_propagacao(PropagacaoViagem(**{**campos, **versoes}))


def consultar(app, metrica="MEDIA", **params):
    app.config["PROPAGACAO_RANKING_METRICA"] = metrica
    return app.test_client().get("/atrasos/ranking", query_string={"mes": "2026-11", **params})


def ordem(resposta):
    return [(d["trecho"]["id"], d["aumento_atraso"]) for d in resposta.get_json()["dados"]]


# --- persistência da T01 ----------------------------------------------------------------

def test_t01_persiste_valor_null_unidade_versoes_e_reprocessa_sem_duplicar(rede):
    propagar(rede, "v1", {"t01": 180.0, "t02": None})
    propagar(rede, "v1", {"t01": 180.0, "t02": None})
    linhas = {r.trecho_id: r for r in IndicadorOperacional.query.filter_by(indicador=INDICADOR_VARIACAO)}
    assert len(linhas) == 2 and IndicadorOperacional.query.count() == 2
    t1, t2 = linhas["t01"], linhas["t02"]
    assert (t1.valor, t1.unidade, t1.versao_calculo, t1.versao_correspondencia) == (
        180.0, "s", "propagacao-atraso-1.0", "match")
    assert (t2.valor, t2.qualidade["motivo"]) == (None, "passagem_ausente")
    assert (t1.qualidade["viagem_observada_id"], t1.qualidade["versao_estimativa"]) == ("v1", "est")
    # Período = dia operacional em São Paulo (00:00 local = 03:00 UTC).
    assert (t1.periodo_inicio.hour, t1.periodo_fim - t1.periodo_inicio) == (3, timedelta(days=1))


# --- agregação e ranking --------------------------------------------------------------------

def test_ordena_decrescente_e_limita_a_vinte(app, rede):
    propagar(rede, "v1", {f"t{i:02d}": float(10 * i) for i in range(1, 26)})
    agregar_mes("2026-11", "MEDIA")
    corpo = consultar(app).get_json()
    assert [d["trecho"]["id"] for d in corpo["dados"]] == [f"t{i:02d}" for i in range(25, 5, -1)]
    assert [d["posicao"] for d in corpo["dados"]] == list(range(1, 21))
    assert (corpo["trechos_com_valor"], corpo["limite"]) == (25, 20)


def test_menos_de_vinte_retorna_so_os_disponiveis(app, rede):
    propagar(rede, "v1", {"t01": 30.0, "t02": -60.0, "t03": 90.0})
    agregar_mes("2026-11", "MEDIA")
    assert ordem(consultar(app)) == [("t03", 90.0), ("t01", 30.0), ("t02", -60.0)]


def test_empate_desempata_por_trecho_id(app, rede):
    propagar(rede, "v1", {"t07": 60.0, "t03": 60.0, "t05": 60.0, "t01": 90.0})
    agregar_mes("2026-11", "MEDIA")
    assert [i for i, _ in ordem(consultar(app))] == ["t01", "t03", "t05", "t07"]


def test_null_e_versoes_misturadas_ficam_fora(app, rede):
    propagar(rede, "v1", {"t01": None, "t02": 30.0, "t03": 50.0})
    propagar(rede, "v2", {"t01": None})
    propagar(rede, "v3", {"t03": 70.0}, versao_estimativa="outra")
    agregar_mes("2026-11", "MEDIA")
    corpo = consultar(app).get_json()
    assert ordem(consultar(app)) == [("t02", 30.0)]
    assert corpo["trechos_sem_valor"] == 2
    motivos = {r.trecho_id: r.qualidade["motivo_sem_valor"]
               for r in IndicadorOperacional.query.filter_by(indicador=INDICADOR_MENSAL)}
    assert motivos == {"t01": "sem_variacao_calculavel", "t02": None, "t03": "versoes_misturadas"}


def test_media_e_mediana_conferidas_manualmente(app, rede):
    # t01: 180, 60, 0 → média 80, mediana 60 | t02: 0, 120, 600 → média 240, mediana 120
    # t03: -60, NULL, 0 → média -30 e mediana -30 com 2 de 3 amostras (cobertura 2/3)
    for vid, (a, b, c) in {"v1": (180.0, 0.0, -60.0), "v2": (60.0, 120.0, None),
                           "v3": (0.0, 600.0, 0.0)}.items():
        propagar(rede, vid, {"t01": a, "t02": b, "t03": c})
    agregar_mes("2026-11", "MEDIA")
    agregar_mes("2026-11", "MEDIANA")
    assert ordem(consultar(app, "MEDIA")) == [("t02", 240.0), ("t01", 80.0), ("t03", -30.0)]
    assert ordem(consultar(app, "MEDIANA")) == [("t02", 120.0), ("t01", 60.0), ("t03", -30.0)]
    t03 = consultar(app).get_json()["dados"][2]
    assert (t03["amostras"], t03["amostras_total"], t03["cobertura"]) == (2, 3, pytest.approx(2 / 3))
    assert t03["qualidade"]["sem_valor_por_motivo"] == {"passagem_ausente": 1}


def test_mes_sem_resultados(app, rede):
    resposta = consultar(app, mes="2026-10")
    assert resposta.status_code == 200
    assert (resposta.get_json()["dados"], resposta.get_json()["trechos_com_valor"]) == ([], 0)


def test_limites_do_mes_pela_data_operacional(app, rede):
    assert limites_mes("2026-12")[1].isoformat() == "2027-01-01T03:00:00+00:00"
    propagar(rede, "fim-nov", {"t01": 100.0}, data=date(2026, 11, 30))
    propagar(rede, "dez", {"t01": 500.0}, data=date(2026, 12, 1))
    propagar(rede, "out", {"t01": 900.0}, data=date(2026, 10, 31))
    agregar_mes("2026-11", "MEDIA")
    agregar_mes("2026-12", "MEDIA")
    assert ordem(consultar(app)) == [("t01", 100.0)]
    assert ordem(consultar(app, mes="2026-12")) == [("t01", 500.0)]


def test_filtros_e_sentidos_nao_se_misturam(app, rede):
    rede["trechos"]["t02"].sentido = "1"
    db.session.commit()
    propagar(rede, "v1", {"t01": 10.0})
    # Viagem de sentido 1 publica com o sentido da sua importação.
    rede["importacao"].sentido = "1"
    db.session.commit()
    propagar(rede, "v2", {"t02": 20.0})
    agregar_mes("2026-11", "MEDIA")
    assert ordem(consultar(app, sentido="0")) == [("t01", 10.0)]
    assert ordem(consultar(app, sentido="1")) == [("t02", 20.0)]
    assert ordem(consultar(app, linha="outra")) == []


def test_correlacao_nao_entra_no_ranking(app, rede):
    propagar(rede, "v1", {"t01": 10.0})
    agregar_mes("2026-11", "MEDIA")
    inicio, fim = limites_mes("2026-11")
    db.session.add(IndicadorOperacional(
        versao_gtfs_id=rede["versao"].id, linha_codigo="3762-10", trecho_id="t02",
        indicador="CORRELACAO_ATRASO_CHEGADA_PARTIDA", valor=0.9, unidade="r (adimensional)",
        periodo_inicio=inicio, periodo_fim=fim, versao_conjunto="x"))
    db.session.commit()
    assert ordem(consultar(app)) == [("t01", 10.0)]


def test_reagregar_nao_duplica_e_api_bate_com_o_persistido(app, rede):
    propagar(rede, "v1", {"t01": 10.0, "t02": 20.0})
    agregar_mes("2026-11", "MEDIA")
    propagar(rede, "v2", {"t01": 30.0})
    agregar_mes("2026-11", "MEDIA")
    persistidos = {r.trecho_id: r for r in IndicadorOperacional.query.filter_by(indicador=INDICADOR_MENSAL)}
    assert len(persistidos) == 2
    for item in consultar(app).get_json()["dados"]:
        r = persistidos[item["trecho"]["id"]]
        assert (item["aumento_atraso"], item["unidade"], item["amostras"], item["versao_calculo"],
                item["versao_conjunto"]) == (r.valor, r.unidade, r.contagem_numerador, r.versao_calculo,
                                             r.versao_conjunto)
    assert persistidos["t01"].valor == 20.0


# --- contrato HTTP ------------------------------------------------------------------------

@pytest.mark.parametrize("params", [
    {"mes": "2026-13"}, {"mes": "2026/11"}, {"mes": ""}, {"mes": "26-11"}, {"mes": "0000-01"},
    {"sentido": "2"}, {"linha": "x" * 70}, {"pagina": "1"},
])
def test_parametros_invalidos(app, rede, params):
    resposta = consultar(app, **params)
    assert resposta.status_code == 400
    assert resposta.get_json()["erro"]["codigo"] == "PARAMETRO_INVALIDO"


def test_mes_obrigatorio_e_repetido(app, rede):
    app.config["PROPAGACAO_RANKING_METRICA"] = "MEDIA"
    cliente = app.test_client()
    assert cliente.get("/atrasos/ranking").status_code == 400
    assert cliente.get("/atrasos/ranking?mes=2026-11&mes=2026-10").status_code == 400
    assert cliente.get("/api/atrasos/ranking?mes=2026-11").status_code == 200


def test_metrica_pendente_do_po(app, rede):
    assert app.config["PROPAGACAO_RANKING_METRICA"] is None
    with pytest.raises(MetricaMensalIndefinida):
        agregar_mes("2026-11")
    resposta = app.test_client().get("/atrasos/ranking?mes=2026-11")
    assert resposta.status_code == 409
    assert resposta.get_json()["erro"]["candidatas"] == ["MEDIA", "MEDIANA"]


# --- Fluxo A completo com o cálculo real da T01 ------------------------------------------------

def test_fluxo_a_da_passagem_ao_ranking(app, programacao):
    """Percurso A→B→C→D, previsto a cada 4 min. Atrasos por parada (s):

    09:00  A=120 B=300 C=300 D=240 → AB +180, BC 0,    CD −60
    09:15  A=0   B=0   C=600 D=600 → AB 0,    BC +600, CD 0
    09:30  A=60  B=120 C=240 D=—   → AB +60,  BC +120, CD NULL (passagem_ausente)
    Média: BC 240, AB 80, CD −30 (2 de 3).
    """
    versao, linha, importacao, partidas = programacao
    lon = {"A": -46.700, "B": -46.697, "C": -46.694, "D": -46.690}
    db.session.add_all(Parada(versao_gtfs_id=versao.id, codigo=c, nome=c, latitude=Decimal("-23.55"),
                              longitude=Decimal(str(x))) for c, x in lon.items())
    trechos = [Trecho(id=f"T-{o}{d}", versao_gtfs_id=versao.id, linha_codigo=linha.codigo, sentido="0",
                      trajeto="T1", ordem=i + 1, parada_origem_codigo=o, parada_destino_codigo=d,
                      geometria=[[lon[o], -23.55], [lon[d], -23.55]])
               for i, (o, d) in enumerate(zip("ABC", "BCD"))]
    db.session.add_all(trechos)
    atrasos = [{"A": 120, "B": 300, "C": 300, "D": 240}, {"A": 0, "B": 0, "C": 600, "D": 600},
               {"A": 60, "B": 120, "C": 240}]
    for partida, por_parada in zip(partidas, atrasos):
        viagem = ViagemObservada(partida_programada=partida, saida_observada=partida.saida_prevista,
                                 cobertura_tracado=1, versao_reconstrucao="recon-sint",
                                 versao_correspondencia="match-sint")
        db.session.add(viagem)
        db.session.flush()
        local = partida.saida_prevista.astimezone(FUSO)
        previstos = {c: (local + timedelta(minutes=4 * i)).strftime("%H:%M:%S") for i, c in enumerate(lon)}
        passagens = [PassagemEstimada(id=f"{viagem.id}-{c}", viagem_observada_id=viagem.id,
                                      versao_gtfs_id=versao.id, parada_codigo=c,
                                      instante=partida.saida_prevista + timedelta(minutes=4 * i, seconds=s),
                                      metodo="INTERPOLACAO_LINEAR_PERCURSO", versao_estimativa="est-sint",
                                      posicao_anterior_id=1, posicao_posterior_id=2)
                     for i, (c, s) in enumerate((c, por_parada.get(c)) for c in lon) if s is not None]
        publicar_propagacao(calcular_propagacao(viagem, trechos, passagens, previstos))
    agregar_mes("2026-11", "MEDIA")
    corpo = consultar(app).get_json()
    assert ordem(consultar(app)) == [("T-BC", 240.0), ("T-AB", 80.0), ("T-CD", -30.0)]
    primeiro = corpo["dados"][0]
    assert (primeiro["trecho"]["parada_origem"], primeiro["trecho"]["parada_destino"]) == ("B", "C")
    assert primeiro["qualidade"]["versoes_entrada"] == [{
        "versao_gtfs_id": versao.id, "versao_calculo": "propagacao-atraso-1.0",
        "versao_correspondencia": "match-sint", "versao_reconstrucao": "recon-sint",
        "versao_estimativa": "est-sint"}]
    assert (corpo["dados"][2]["amostras"], corpo["dados"][2]["amostras_total"]) == (2, 3)
