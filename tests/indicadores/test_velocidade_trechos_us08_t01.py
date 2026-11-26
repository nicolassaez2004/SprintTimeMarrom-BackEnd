"""US08-T01: tempo de percurso e velocidade por trecho a partir das passagens da US15-T01.

Percurso reto em latitude -23.55: A(-46.700) → B(-46.697) → C(-46.694) →
D(-46.690). Posições sobre as paradas: as passagens recebem o horário GPS.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace as NS

import pytest

from app import db
from app.contexto.trafego import (
    INVALIDA, METODO, PENDENTE, SEM_DADOS, UNIDADE, VALIDA, AvaliadorVelocidade, Limites,
    calcular_viagem, geometria_valida,
)
from app.correspondencia.geografia import PROJECAO
from app.models import (
    ExecucaoColeta, ObservacaoTratada, Parada, PassagemEstimada, RegistroBrutoGPS, Trecho,
    ViagemObservada,
)
from app.viagens.passagens import estimar_e_gravar, versao_estimativa

LAT = -23.55
PARADAS = {"A": -46.700, "B": -46.697, "C": -46.694, "D": -46.690}
LACUNA = timedelta(seconds=120)
VERSAO_EST = versao_estimativa(LACUNA)
COMPLETOS = Limites(LACUNA, 1.0, 80.0)  # valores de ensaio; os oficiais aguardam o PO


def metros(o, d):
    (x1, x2), (y1, y2) = PROJECAO.transform([PARADAS[o], PARADAS[d]], [LAT, LAT])
    return ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5


# --- cálculo isolado (dublês) ------------------------------------------------

T0 = datetime(2026, 11, 24, 12, tzinfo=timezone.utc)
TRECHO = NS(id="tr-1", versao_gtfs_id="v1", linha_codigo="L", sentido="0",
            parada_origem_codigo="A", parada_destino_codigo="B")


def passagem(parada, segundos, *, viagem="vg-1", versao="v1", estimativa=VERSAO_EST, sentido="0"):
    instante = T0 + timedelta(seconds=segundos)
    return NS(id=f"p-{parada}-{viagem}", viagem_observada_id=viagem, parada_codigo=parada,
              versao_gtfs_id=versao, versao_estimativa=estimativa, instante=instante,
              posicao_anterior=NS(horario_gps=instante), posicao_posterior=NS(horario_gps=instante),
              viagem=NS(partida_programada=NS(importacao=NS(
                  versao_gtfs_id="v1", linha_codigo="L", sentido=sentido))))


def avaliar(origem, destino, distancia=500.0, limites=COMPLETOS, instantes=()):
    return AvaliadorVelocidade(limites).avaliar(TRECHO, origem, destino, distancia, instantes)


def test_distancia_e_tempo_conhecidos_em_km_h():
    # 500 m em 60 s = 8,333 m/s = 30 km/h.
    amostra = avaliar(passagem("A", 0), passagem("B", 60))
    assert (amostra.situacao, amostra.motivo) == (VALIDA, None)
    assert amostra.duracao_seg == 60 and amostra.distancia_m == 500
    assert amostra.velocidade_kmh == pytest.approx(30.0)
    assert amostra.unidade == UNIDADE == "km/h"


@pytest.mark.parametrize("metros_, segundos, kmh", [(1000, 3600, 1.0), (1000, 100, 36.0), (250, 45, 20.0)])
def test_conversao_metros_por_segundo_para_km_h(metros_, segundos, kmh):
    amostra = avaliar(passagem("A", 0), passagem("B", segundos), distancia=metros_)
    assert amostra.velocidade_kmh == pytest.approx(kmh)


@pytest.mark.parametrize("segundos, motivo", [(0, "duracao_nula"), (-30, "ordem_temporal_invertida")])
def test_duracao_nula_ou_negativa_e_rejeitada_sem_virar_zero(segundos, motivo):
    amostra = avaliar(passagem("A", 30), passagem("B", 30 + segundos))
    assert (amostra.situacao, amostra.motivo, amostra.velocidade_kmh) == (INVALIDA, motivo, None)
    assert amostra.duracao_seg == segundos  # o valor calculado fica para conferência


def test_movimento_contrario_chega_ao_destino_antes_da_origem():
    # Veículo percorre B → A: passagem em B antes da passagem em A.
    amostra = avaliar(passagem("A", 90), passagem("B", 10))
    assert amostra.motivo == "ordem_temporal_invertida"


def test_passagens_de_viagens_diferentes():
    amostra = avaliar(passagem("A", 0), passagem("B", 60, viagem="vg-2"))
    assert (amostra.situacao, amostra.motivo) == (INVALIDA, "viagens_diferentes")
    assert (amostra.passagem_origem_id, amostra.passagem_destino_id) == ("p-A-vg-1", "p-B-vg-2")


def test_sentido_da_viagem_incompativel_com_o_trecho():
    amostra = avaliar(passagem("A", 0, sentido="1"), passagem("B", 60, sentido="1"))
    assert amostra.motivo == "sentido_incompativel"


@pytest.mark.parametrize("origem, destino", [
    (passagem("A", 0, versao="v2"), passagem("B", 60, versao="v2")),
    (passagem("A", 0), passagem("B", 60, estimativa="us15-t01-v1-lacuna60s")),
])
def test_versao_gtfs_ou_de_estimativa_incompativel(origem, destino):
    assert avaliar(origem, destino).motivo == "versao_incompativel"


def test_passagem_de_outra_parada():
    assert avaliar(passagem("A", 0), passagem("C", 60)).motivo == "parada_incompativel"


@pytest.mark.parametrize("distancia", [None, 0.0, -5.0, float("nan"), float("inf")])
def test_distancia_nula_invalida_ou_indisponivel(distancia):
    amostra = avaliar(passagem("A", 0), passagem("B", 60), distancia=distancia)
    assert (amostra.situacao, amostra.motivo, amostra.velocidade_kmh) == (
        INVALIDA, "distancia_invalida", None)


@pytest.mark.parametrize("geometria", [
    None, [], [[-46.7, LAT]], [[-46.7, LAT], [-46.7, LAT]], [[-46.7, LAT], [200, LAT]],
    [[-46.7, LAT], ["x", LAT]], [[-46.7, LAT], [float("nan"), LAT]],
])
def test_geometria_invalida(geometria):
    assert not geometria_valida(geometria)
    assert geometria_valida([[-46.7, LAT], [-46.69, LAT]])


def test_lacuna_entre_posicoes_acima_do_limite():
    origem, destino = passagem("A", 0), passagem("B", 200)
    # Posições válidas em 0, 40, 70 e 200 s (400 s fica fora): maior intervalo = 130 s.
    instantes = [T0 + timedelta(seconds=s) for s in (40, 70, 400)]
    assert avaliar(origem, destino, instantes=instantes).motivo == "lacuna_temporal"
    amostra = avaliar(origem, destino, limites=Limites(timedelta(seconds=130), 1, 80),
                      instantes=instantes)
    assert amostra.situacao == VALIDA and amostra.lacuna_maxima_observada_seg == 130


@pytest.mark.parametrize("segundos, motivo", [(1, "velocidade_acima_do_maximo"),
                                              (1900, "velocidade_abaixo_do_minimo")])
def test_velocidade_fora_da_plausibilidade(segundos, motivo):
    instantes = [T0 + timedelta(seconds=s) for s in range(0, segundos + 1, 60)]
    amostra = avaliar(passagem("A", 0), passagem("B", segundos), instantes=instantes)
    assert (amostra.situacao, amostra.motivo) == (INVALIDA, motivo)
    assert amostra.velocidade_kmh is not None  # valor rejeitado fica visível, não zero


def test_limites_indefinidos_deixam_a_amostra_pendente():
    origem, destino = passagem("A", 0), passagem("B", 60)
    sem_plaus = avaliar(origem, destino, limites=Limites(LACUNA))
    assert (sem_plaus.situacao, sem_plaus.motivo) == (PENDENTE, "plausibilidade_indefinida")
    assert sem_plaus.velocidade_kmh == pytest.approx(30.0) and not sem_plaus.valida
    sem_lacuna = avaliar(origem, destino, limites=Limites(None, 1, 80))
    assert sem_lacuna.motivo == "lacuna_maxima_indefinida"
    assert Limites().versao == "us08-t01-v1-lacunaindef-vminindef-vmaxindef"
    with pytest.raises(ValueError):
        Limites(LACUNA, 50, 10)


def test_velocidade_baixa_valida_invalida_e_ausencia_sao_distintas():
    lento = avaliar(passagem("A", 0), passagem("B", 120), distancia=1.0,
                    limites=Limites(LACUNA, 0.0, 80))
    assert lento.situacao == VALIDA and 0 < lento.velocidade_kmh < 0.1
    invalida = avaliar(passagem("A", 0), passagem("B", 0))
    ausente = avaliar(passagem("A", 0), None)
    assert (invalida.situacao, invalida.velocidade_kmh) == (INVALIDA, None)
    assert (ausente.situacao, ausente.motivo, ausente.velocidade_kmh) == (
        SEM_DADOS, "passagem_destino_ausente", None)
    assert ausente.viagem_observada_id == "vg-1"
    assert avaliar(None, None).motivo == "passagem_origem_ausente"


# --- integração com as passagens gravadas pela US15-T01 ----------------------

@pytest.fixture
def cenario(programacao):
    versao, linha, importacao, partidas = programacao
    db.session.add_all(Parada(versao_gtfs_id=versao.id, codigo=c, nome=f"Parada {c}",
                              latitude=Decimal(str(LAT)), longitude=Decimal(str(lon)))
                       for c, lon in PARADAS.items())
    codigos = list(PARADAS)
    db.session.add_all(
        Trecho(versao_gtfs_id=versao.id, linha_codigo=linha.codigo, sentido="0", trajeto="T1",
               ordem=i + 1, parada_origem_codigo=o, parada_destino_codigo=d,
               geometria=[[PARADAS[o], LAT], [PARADAS[d], LAT]])
        for i, (o, d) in enumerate(zip(codigos, codigos[1:])))
    viagem = ViagemObservada(partida_programada_id=partidas[0].id,
                             saida_observada=partidas[0].saida_prevista, cobertura_tracado=1,
                             versao_reconstrucao="recon-sint", versao_correspondencia="match-sint")
    execucao = ExecucaoColeta(iniciada_em=partidas[0].saida_prevista, estado="OK")
    db.session.add_all([viagem, execucao])
    db.session.commit()
    return NS(versao=versao, linha=linha, viagem=viagem, execucao=execucao,
              t0=partidas[0].saida_prevista)


def gravar(c, *pontos):
    """``pontos``: (segundos, longitude) válidos; estima e grava as passagens."""
    obs = []
    for segundos, lon in pontos:
        bruto = RegistroBrutoGPS(execucao=c.execucao, prefixo_origem="5810", linha_origem="3762-10",
                                 latitude=LAT, longitude=lon,
                                 horario_gps=c.t0 + timedelta(seconds=segundos))
        db.session.add(bruto)
        obs.append(ObservacaoTratada(bruto=bruto, valida=True, versao_correspondencia="match-sint",
                                     versao_gtfs_id=c.versao.id, linha_codigo=c.linha.codigo,
                                     sentido_gtfs="0"))
        db.session.add_all([bruto, obs[-1]])
    db.session.flush()
    estimar_e_gravar(c.viagem, obs, lacuna_maxima=LACUNA)
    db.session.commit()


def test_amostras_por_trecho_com_passagens_reais(cenario):
    gravar(cenario, (0, PARADAS["A"]), (30, PARADAS["B"]), (90, PARADAS["C"]))
    amostras, motivo = calcular_viagem(cenario.viagem, VERSAO_EST, COMPLETOS)
    assert motivo is None and len(amostras) == 3
    ab, bc, cd = amostras
    assert ab.situacao == bc.situacao == VALIDA
    assert ab.distancia_m == pytest.approx(metros("A", "B"), abs=0.01)
    assert ab.velocidade_kmh == pytest.approx(metros("A", "B") / 30 * 3.6, rel=1e-6)
    assert bc.velocidade_kmh == pytest.approx(metros("B", "C") / 60 * 3.6, rel=1e-6)
    assert (cd.situacao, cd.motivo) == (SEM_DADOS, "passagem_destino_ausente")


def test_preserva_referencias_de_origem_e_versoes(cenario):
    gravar(cenario, (0, PARADAS["A"]), (30, PARADAS["B"]))
    ab = calcular_viagem(cenario.viagem, VERSAO_EST, COMPLETOS)[0][0]
    p = {x.parada_codigo: x for x in PassagemEstimada.query.filter_by(viagem_observada_id=cenario.viagem.id)}
    trecho = Trecho.query.filter_by(ordem=1).one()
    assert (ab.trecho_id, ab.viagem_observada_id, ab.versao_gtfs_id) == (
        trecho.id, cenario.viagem.id, cenario.versao.id)
    assert (ab.passagem_origem_id, ab.passagem_destino_id) == (p["A"].id, p["B"].id)
    assert (ab.instante_origem, ab.instante_destino) == (p["A"].instante, p["B"].instante)
    assert (ab.versao_estimativa, ab.metodo, ab.unidade) == (VERSAO_EST, METODO, "km/h")
    assert ab.versao_calculo == "us08-t01-v1-lacuna120-vmin1-vmax80"
    dados = ab.como_dict()
    assert dados["instante_origem"] == p["A"].instante.isoformat() and dados["situacao"] == VALIDA


def test_lacuna_do_config_e_heranca_da_estimativa(cenario, app):
    gravar(cenario, (0, PARADAS["A"]), (30, PARADAS["B"]), (130, PARADAS["C"]))
    app.config.update(VELOCIDADE_MINIMA_KMH="1", VELOCIDADE_MAXIMA_KMH="80")
    # Sem valor próprio: herda os 120 s da estimativa → B→C (100 s) é aceito.
    herdado = calcular_viagem(cenario.viagem, VERSAO_EST)[0]
    assert herdado[1].situacao == VALIDA and herdado[1].versao_calculo.startswith("us08-t01-v1-lacuna120")
    app.config["VELOCIDADE_LACUNA_MAXIMA_SEG"] = "60"
    restrito = calcular_viagem(cenario.viagem, VERSAO_EST)[0]
    assert (restrito[0].situacao, restrito[1].motivo) == (VALIDA, "lacuna_temporal")
    app.config.update(VELOCIDADE_LACUNA_MAXIMA_SEG=None, VELOCIDADE_MAXIMA_KMH=None)
    assert calcular_viagem(cenario.viagem, VERSAO_EST)[0][0].situacao == PENDENTE


def test_geometria_invalida_do_trecho_nao_produz_velocidade(cenario):
    gravar(cenario, (0, PARADAS["A"]), (30, PARADAS["B"]), (90, PARADAS["C"]))
    trecho = Trecho.query.filter_by(ordem=2).one()
    trecho.geometria = [[PARADAS["B"], LAT], [PARADAS["B"], LAT]]
    db.session.commit()
    amostras = calcular_viagem(cenario.viagem, VERSAO_EST, COMPLETOS)[0]
    assert (amostras[1].situacao, amostras[1].motivo, amostras[1].velocidade_kmh) == (
        INVALIDA, "distancia_invalida", None)


def test_sem_estimativa_nao_afirma_ausencia(cenario):
    assert calcular_viagem(cenario.viagem, VERSAO_EST, COMPLETOS) == ((), "estimativa_ausente")


def test_nao_altera_brutos_nem_passagens(cenario):
    gravar(cenario, (0, PARADAS["A"]), (30, PARADAS["B"]))
    antes = [(r.id, r.latitude, r.longitude, r.horario_gps) for r in RegistroBrutoGPS.query]
    passagens = PassagemEstimada.query.count()
    calcular_viagem(cenario.viagem, VERSAO_EST, COMPLETOS)
    assert not db.session.new and not db.session.dirty
    assert antes == [(r.id, r.latitude, r.longitude, r.horario_gps) for r in RegistroBrutoGPS.query]
    assert PassagemEstimada.query.count() == passagens
