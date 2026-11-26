"""US19-T02: chegada de uma viagem × partida seguinte do mesmo veículo (dados sintéticos).

Base: 24/11/2026 12:00 UTC = 09:00 em São Paulo. Intervalo máximo de teste: 30 min
(valor sintético; o limite real é decisão pendente do PO).
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app import db
from app.indicadores.propagacao import (
    INDICADOR_CORRELACAO, UNIDADE_CORRELACAO, VERSAO_CALCULO, VERSAO_CORRELACAO,
    IntervaloMaximoIndefinido, ViagemVeiculo, calcular_propagacao,
    correlacionar_chegada_partida, montar_correlacao, publicar_correlacao, viagem_do_veiculo,
)
from app.models import (
    ExecucaoColeta, IndicadorOperacional, ObservacaoTratada, Parada, PassagemEstimada,
    RegistroBrutoGPS, Trecho, ViagemObservada,
)
from app.viagens.passagens import estimar_e_gravar

T0 = datetime(2026, 11, 24, 12, tzinfo=timezone.utc)
MAX = timedelta(minutes=30)


def vv(viagem_id, saida, chegada, ac=0.0, ap=0.0, prefixos=("5810",), **mudancas):
    """Viagem com saída/chegada em minutos após T0 e atrasos (s) de chegada/partida."""
    s = T0 + timedelta(minutes=saida)
    c = None if chegada is None else T0 + timedelta(minutes=chegada)
    campos = dict(
        viagem_id=viagem_id, partida_programada_id=f"p-{viagem_id}", prefixos=frozenset(prefixos),
        linha_codigo="3762-10", sentido="0", versao_gtfs_id="g1", data_operacao=date(2026, 11, 24),
        saida=s, atraso_partida_seg=ap, motivo_partida=None if ap is not None else "OFERTA_POR_FREQUENCIA",
        chegada=c, atraso_chegada_seg=ac,
        motivo_chegada=None if ac is not None else "horario_previsto_ausente",
        termino=c, versao_reconstrucao="recon", versao_correspondencia="match", versao_estimativa="est",
    )
    return ViagemVeiculo(**{**campos, **mudancas})


def ids(pares):
    return [(p.anterior.viagem_id, p.seguinte.viagem_id) for p in pares]


def motivos(resultado):
    return {(p.anterior.viagem_id, p.seguinte.viagem_id): p.motivo for p in resultado.descartados}


def serie(xs, ys):
    """Viagens 0..n de um veículo, espaçadas 20 min; chegada aos 10 min de cada uma."""
    n = len(xs) + 1
    ac, ap = list(xs) + [0.0], [0.0] + list(ys)
    return [vv(f"v{i}", 20 * i, 20 * i + 10, ac=ac[i], ap=ap[i]) for i in range(n)]


# --- pareamento ------------------------------------------------------------------------

def test_duas_viagens_consecutivas_do_mesmo_veiculo(app):
    r = correlacionar_chegada_partida([vv("a", 0, 40, ac=120), vv("b", 55, 90, ap=180)], MAX)
    assert ids(r.pares) == [("a", "b")] and r.descartados == ()
    par = r.pares[0].registro()
    assert (par["prefixo"], par["intervalo_seg"], par["atraso_chegada_seg"], par["atraso_partida_seg"]) == (
        "5810", 900, 120, 180)


def test_prefixos_diferentes_nunca_sao_pareados(app):
    r = correlacionar_chegada_partida([vv("a", 0, 40), vv("b", 55, 90, prefixos=("9999",))], MAX)
    assert r.pares == r.descartados == ()


def test_so_o_vizinho_imediato_e_candidato(app):
    # b não tem atraso de partida: a–b é descartado e a–c não é formado.
    r = correlacionar_chegada_partida([vv("a", 0, 10), vv("b", 20, 30, ap=None), vv("c", 40, 50)], MAX)
    assert ids(r.pares) == [("b", "c")]
    assert motivos(r) == {("a", "b"): "partida_sem_atraso"}


def test_troca_de_veiculo_descarta_e_nao_e_pulada(app):
    viagens = [vv("a", 0, 10), vv("b", 20, 30, prefixos=("5810", "7777")), vv("c", 40, 50)]
    r = correlacionar_chegada_partida(viagens, MAX)
    assert r.pares == ()
    assert motivos(r) == {("a", "b"): "troca_de_veiculo", ("b", "c"): "troca_de_veiculo"}


def test_viagens_sobrepostas_e_fora_de_ordem(app):
    viagens = [vv("b", 30, 60), vv("a", 0, 40)]  # b sai antes da chegada de a
    r = correlacionar_chegada_partida(viagens, MAX)
    assert motivos(r) == {("a", "b"): "viagens_sobrepostas"}
    assert r.pares == ()
    mesma_saida = correlacionar_chegada_partida([vv("a", 0, 10), vv("b", 0, 10)], MAX)
    assert motivos(mesma_saida) == {("a", "b"): "viagens_sobrepostas"}


def test_sobreposicao_usa_fim_observado_sem_chegada(app):
    a = vv("a", 0, None, ac=None, termino=T0 + timedelta(minutes=40))
    r = correlacionar_chegada_partida([a, vv("b", 30, 60)], MAX)
    assert motivos(r) == {("a", "b"): "viagens_sobrepostas"}


def test_lacuna_excessiva(app):
    r = correlacionar_chegada_partida([vv("a", 0, 10), vv("b", 41, 60)], MAX)
    assert motivos(r) == {("a", "b"): "lacuna_excessiva"}
    limite = correlacionar_chegada_partida([vv("a", 0, 10), vv("b", 40, 60)], MAX)
    assert ids(limite.pares) == [("a", "b")]  # 30 min exatos: dentro do limite


def test_sem_chegada_ou_partida_confiavel(app):
    r = correlacionar_chegada_partida([vv("a", 0, None, ac=None), vv("b", 20, 30)], MAX)
    assert motivos(r) == {("a", "b"): "chegada_sem_atraso"}
    assert r.descartados[0].registro()["detalhe"] == "horario_previsto_ausente"
    r = correlacionar_chegada_partida([vv("a", 0, 10), vv("b", 20, 30, ap=None)], MAX)
    assert r.descartados[0].registro()["detalhe"] == "OFERTA_POR_FREQUENCIA"


def test_sem_horario_programado_na_chegada(app):
    # Passagem existe, mas a T01 não tem horário previsto no terminal.
    r = correlacionar_chegada_partida([vv("a", 0, 10, ac=None), vv("b", 20, 30)], MAX)
    assert motivos(r) == {("a", "b"): "chegada_sem_atraso"}


def test_horario_inconsistente(app):
    a = vv("a", 10, 5)  # chegada antes da própria saída
    r = correlacionar_chegada_partida([a, vv("b", 20, 30)], MAX)
    assert motivos(r) == {("a", "b"): "horario_inconsistente"}


def test_linha_ou_versao_gtfs_diferente(app):
    r = correlacionar_chegada_partida([vv("a", 0, 10), vv("b", 20, 30, linha_codigo="8000-10")], MAX)
    assert motivos(r) == {("a", "b"): "linha_diferente"}
    r = correlacionar_chegada_partida([vv("a", 0, 10), vv("b", 20, 30, versao_gtfs_id="g2")], MAX)
    assert motivos(r) == {("a", "b"): "versao_gtfs_diferente"}


def test_veiculo_nao_identificado_e_excluido(app):
    r = correlacionar_chegada_partida([vv("a", 0, 10, prefixos=()), vv("b", 20, 30, prefixos=(None,))], MAX)
    assert r.excluidas == (("a", "veiculo_nao_identificado"), ("b", "veiculo_nao_identificado"))
    assert r.pares == r.descartados == ()


def test_viagem_atravessando_a_meia_noite(app):
    # Chegada 23:50 de 24/11 (SP) e partida 00:10 de 25/11 (SP), dias operacionais distintos.
    a = vv("a", 14 * 60 + 30, 14 * 60 + 50)
    b = vv("b", 15 * 60 + 10, 15 * 60 + 40, data_operacao=date(2026, 11, 25))
    r = correlacionar_chegada_partida([a, b], MAX)
    par = r.pares[0].registro()
    assert (par["intervalo_seg"], par["data_operacao_chegada"], par["data_operacao_partida"]) == (
        1200, "2026-11-24", "2026-11-25")
    assert par["chegada"] == "2026-11-25T02:50:00+00:00"


def test_versoes_misturadas_sao_rejeitadas(app):
    with pytest.raises(ValueError, match="reconstrução"):
        correlacionar_chegada_partida([vv("a", 0, 10), vv("b", 20, 30, versao_reconstrucao="x")], MAX)
    with pytest.raises(ValueError, match="estimativa"):
        correlacionar_chegada_partida([vv("a", 0, 10), vv("b", 20, 30, versao_estimativa="x")], MAX)
    with pytest.raises(ValueError, match="repetida"):
        correlacionar_chegada_partida([vv("a", 0, 10), vv("a", 20, 30)], MAX)


def test_intervalo_maximo_indefinido_nao_forma_pares(app):
    assert app.config["PROPAGACAO_INTERVALO_MAXIMO_SEG"] is None
    with pytest.raises(IntervaloMaximoIndefinido):
        correlacionar_chegada_partida([vv("a", 0, 10), vv("b", 20, 30)])
    app.config["PROPAGACAO_INTERVALO_MAXIMO_SEG"] = "1800"
    assert len(correlacionar_chegada_partida([vv("a", 0, 10), vv("b", 20, 30)]).pares) == 1


# --- correlação -------------------------------------------------------------------------

def test_correlacao_positiva_e_negativa(app):
    assert correlacionar_chegada_partida(serie([60, 120, 180], [30, 90, 150]), MAX).coeficiente == pytest.approx(1)
    assert correlacionar_chegada_partida(serie([60, 120, 180], [150, 90, 30]), MAX).coeficiente == pytest.approx(-1)


def test_correlacao_conferida_manualmente(app):
    # x = 60,120,180,240 ; y = 120,60,240,180 → médias 150 e 150
    # dx = -90,-30,30,90 ; dy = -30,-90,90,30 → Σdxdy = 2700+2700+2700+2700 = 10800
    # Σdx² = Σdy² = 8100+900+900+8100 = 18000 → r = 10800 / 18000 = 0,6
    r = correlacionar_chegada_partida(serie([60, 120, 180, 240], [120, 60, 240, 180]), MAX)
    assert (r.amostra, r.coeficiente, r.motivo_sem_valor) == (4, pytest.approx(0.6), None)


def test_amostra_insuficiente_fica_nula(app):
    r = correlacionar_chegada_partida(serie([60, 120], [30, 90]), MAX)
    assert (r.amostra, r.coeficiente, r.motivo_sem_valor) == (2, None, "amostra_insuficiente")
    vazio = correlacionar_chegada_partida([], MAX)
    assert (vazio.amostra, vazio.coeficiente, vazio.motivo_sem_valor) == (0, None, "amostra_insuficiente")


def test_variancia_zero_fica_nula(app):
    r = correlacionar_chegada_partida(serie([120, 120, 120], [30, 90, 150]), MAX)
    assert (r.coeficiente, r.motivo_sem_valor) == (None, "variancia_zero")
    r = correlacionar_chegada_partida(serie([30, 90, 150], [0, 0, 0]), MAX)
    assert (r.coeficiente, r.motivo_sem_valor) == (None, "variancia_zero")


def test_contagens_de_pares_validos_e_descartados(app, programacao):
    g = programacao[0].id
    atrasos = [(60, 0), (120, 30), (180, 90), (0, 150)]
    viagens = [vv(f"v{i}", 20 * i, 20 * i + 10, ac=x, ap=y, versao_gtfs_id=g)
               for i, (x, y) in enumerate(atrasos)]
    viagens += [vv("y", 80, 100, ap=None, versao_gtfs_id=g),
                vv("z", 25, 35, prefixos=("9999",), versao_gtfs_id=g)]
    r = correlacionar_chegada_partida(viagens, MAX)
    # 5810 por saída: v0(0) v1(20) v2(40) v3(60) y(80); 9999 só tem z.
    assert ids(r.pares) == [("v0", "v1"), ("v1", "v2"), ("v2", "v3")]
    assert motivos(r) == {("v3", "y"): "partida_sem_atraso"}
    campos = montar_correlacao(r, linha="3762-10", inicio=T0, fim=T0 + timedelta(days=1),
                               versao_gtfs_id=g)
    assert (campos["contagem_numerador"], campos["contagem_denominador"]) == (3, 4)
    assert campos["valor"] == pytest.approx(1)
    assert campos["qualidade"]["descartes_por_motivo"] == {"partida_sem_atraso": 1}


# --- integração: US15-T01 + US19-T01 + US16 + persistência -------------------------------

LAT = -23.55
PARADAS = {"A": -46.700, "B": -46.697, "C": -46.694, "D": -46.690}


@pytest.fixture
def duas_viagens(programacao):
    """Veículo 5810: partida 09:00 (sai 09:01, chega D 09:14) e partida 09:30 (sai 09:33).

    Previsto 4 min entre paradas → atraso de chegada da 1ª = 120 s, atraso de
    partida da 2ª = 180 s, intervalo chegada → partida = 19 min.
    """
    versao, linha, importacao, partidas = programacao
    db.session.add_all(Parada(versao_gtfs_id=versao.id, codigo=c, nome=c, latitude=Decimal(str(LAT)),
                              longitude=Decimal(str(lon))) for c, lon in PARADAS.items())
    codigos = list(PARADAS)
    trechos = [Trecho(versao_gtfs_id=versao.id, linha_codigo=linha.codigo, sentido="0", trajeto="T1",
                      ordem=i + 1, parada_origem_codigo=o, parada_destino_codigo=d,
                      geometria=[[PARADAS[o], LAT], [PARADAS[d], LAT]])
               for i, (o, d) in enumerate(zip(codigos, codigos[1:]))]
    execucao = ExecucaoColeta(iniciada_em=T0, estado="OK")
    db.session.add_all(trechos + [execucao])
    viagens, brutos = [], []
    for partida, minutos, inicio in ((partidas[0], (1, 5, 9, 14), "09:00"),
                                     (partidas[2], (33, 37, 41, 46), "09:30")):
        viagem = ViagemObservada(partida_programada=partida,
                                 saida_observada=T0 + timedelta(minutes=minutos[0]),
                                 fim_observado=T0 + timedelta(minutes=minutos[-1]), cobertura_tracado=1,
                                 versao_reconstrucao="recon-sint", versao_correspondencia="match-sint")
        db.session.add(viagem)
        obs = []
        for minuto, lon in zip(minutos, PARADAS.values()):
            bruto = RegistroBrutoGPS(execucao=execucao, prefixo_origem="5810", linha_origem="3762-10",
                                     latitude=LAT, longitude=lon, horario_gps=T0 + timedelta(minutes=minuto))
            brutos.append(bruto)
            obs.append(ObservacaoTratada(bruto=bruto, valida=True, versao_correspondencia="match-sint",
                                         versao_gtfs_id=versao.id, linha_codigo="3762-10", sentido_gtfs="0"))
        db.session.add_all(brutos + obs)
        db.session.flush()
        estimar_e_gravar(viagem, obs, timedelta(minutes=10))
        hora = int(inicio[:2]), int(inicio[3:])
        previstos = {c: f"{hora[0]:02d}:{hora[1] + 4 * i:02d}:00" for i, c in enumerate(PARADAS)}
        passagens = PassagemEstimada.query.filter_by(viagem_observada_id=viagem.id).all()
        viagens.append(viagem_do_veiculo(viagem, calcular_propagacao(viagem, trechos, passagens, previstos)))
    db.session.commit()
    return {"versao": versao, "viagens": viagens, "brutos": brutos}


def test_integra_t01_us16_e_gps_bruto(duas_viagens):
    primeira, segunda = duas_viagens["viagens"]
    assert primeira.prefixos == segunda.prefixos == frozenset({"5810"})
    assert (primeira.atraso_chegada_seg, primeira.chegada) == (120, T0 + timedelta(minutes=14))
    assert (segunda.atraso_partida_seg, segunda.saida) == (180, T0 + timedelta(minutes=33))
    antes = [(b.prefixo_origem, b.horario_gps, b.latitude, b.longitude) for b in duas_viagens["brutos"]]
    r = correlacionar_chegada_partida(duas_viagens["viagens"], MAX)
    assert [p.registro()["intervalo_seg"] for p in r.pares] == [19 * 60]
    assert [(b.prefixo_origem, b.horario_gps, b.latitude, b.longitude) for b in duas_viagens["brutos"]] == antes


def test_resultado_de_outra_viagem_e_rejeitado(duas_viagens, programacao):
    viagem = ViagemObservada.query.order_by(ViagemObservada.saida_observada).first()
    outra = ViagemObservada.query.order_by(ViagemObservada.saida_observada.desc()).first()
    trechos = Trecho.query.order_by(Trecho.ordem).all()
    propagacao = calcular_propagacao(outra, trechos, [], {})
    with pytest.raises(ValueError, match="outra viagem"):
        viagem_do_veiculo(viagem, propagacao)


def test_publica_versoes_rastreabilidade_e_reprocessa_sem_duplicar(duas_viagens):
    versao = duas_viagens["versao"]
    contexto = dict(linha="3762-10", inicio=T0, fim=T0 + timedelta(days=1), versao_gtfs_id=versao.id)
    r = correlacionar_chegada_partida(duas_viagens["viagens"], MAX)
    registro = publicar_correlacao(r, **contexto)
    assert (registro.indicador, registro.unidade, registro.valor) == (
        INDICADOR_CORRELACAO, UNIDADE_CORRELACAO, None)
    assert (registro.contagem_numerador, registro.contagem_denominador, registro.sentido) == (1, 1, None)
    assert (registro.versao_calculo, registro.versao_correspondencia) == (VERSAO_CORRELACAO, "match-sint")
    q = registro.qualidade
    assert (q["metodo"], q["situacao_metodo"], q["amostra_minima"], q["motivo_sem_valor"]) == (
        "pearson", "PROPOSTA_PENDENTE_PO", 3, "amostra_insuficiente")
    assert (q["versao_reconstrucao"], q["versao_atraso_chegada"], q["intervalo_maximo_seg"]) == (
        "recon-sint", VERSAO_CALCULO, 1800)
    assert q["versao_estimativa"].startswith("us15-t01")
    par = q["pares_validos"][0]
    primeira, segunda = duas_viagens["viagens"]
    assert (par["viagem_chegada"], par["viagem_partida"], par["prefixo"]) == (
        primeira.viagem_id, segunda.viagem_id, "5810")
    assert (par["partida_programada_chegada"], par["atraso_chegada_seg"], par["atraso_partida_seg"]) == (
        primeira.partida_programada_id, 120, 180)
    assert "falhas_coleta" in q

    publicar_correlacao(correlacionar_chegada_partida(duas_viagens["viagens"], MAX), **contexto)
    assert IndicadorOperacional.query.filter_by(indicador=INDICADOR_CORRELACAO).count() == 1


def test_publicacao_rejeita_par_fora_da_linha_ou_periodo(duas_viagens):
    r = correlacionar_chegada_partida(duas_viagens["viagens"], MAX)
    versao = duas_viagens["versao"].id
    with pytest.raises(ValueError, match="fora da linha"):
        montar_correlacao(r, linha="outra", inicio=T0, fim=T0 + timedelta(days=1), versao_gtfs_id=versao)
    with pytest.raises(ValueError, match="fora da linha"):
        montar_correlacao(r, linha="3762-10", inicio=T0 + timedelta(hours=1),
                          fim=T0 + timedelta(days=1), versao_gtfs_id=versao)


def test_motivo_da_chegada_e_o_da_ultima_parada(duas_viagens):
    """Revisão: antes usava o motivo do trecho, que podia ser o da parada de origem."""
    viagem = ViagemObservada.query.order_by(ViagemObservada.saida_observada).first()
    trechos = Trecho.query.order_by(Trecho.ordem).all()
    passagens = [p for p in PassagemEstimada.query.filter_by(viagem_observada_id=viagem.id)
                 if p.parada_codigo != "C"]
    previstos = {"A": "09:00:00", "B": "09:04:00", "C": "09:08:00"}  # D sem horário previsto
    chegada = viagem_do_veiculo(viagem, calcular_propagacao(viagem, trechos, passagens, previstos))
    assert (chegada.atraso_chegada_seg, chegada.motivo_chegada) == (None, "horario_previsto_ausente")


def test_intervalo_zero_explicito_e_rejeitado(app):
    app.config["PROPAGACAO_INTERVALO_MAXIMO_SEG"] = "1800"
    with pytest.raises(ValueError, match="positivo"):
        correlacionar_chegada_partida([vv("a", 0, 10), vv("b", 20, 30)], timedelta(0))


def test_descartado_de_outra_linha_nao_entra_no_denominador(app, programacao):
    """Revisão: par descartado cuja chegada é de outra linha inflava o denominador."""
    g = programacao[0].id
    viagens = [vv(f"v{i}", 20 * i, 20 * i + 10, ac=x, versao_gtfs_id=g) for i, x in enumerate((60, 120, 180, 0))]
    viagens += [vv("o1", 0, 10, prefixos=("9999",), linha_codigo="8000-10", versao_gtfs_id=g),
                vv("o2", 20, 30, prefixos=("9999",), ap=None, linha_codigo="8000-10", versao_gtfs_id=g)]
    r = correlacionar_chegada_partida(viagens, MAX)
    assert len(r.descartados) == 1
    campos = montar_correlacao(r, linha="3762-10", inicio=T0, fim=T0 + timedelta(days=1), versao_gtfs_id=g)
    assert (campos["contagem_numerador"], campos["contagem_denominador"]) == (3, 3)
    assert campos["qualidade"]["descartados_fora_do_escopo"] == 1
