"""US19-T01: variação do atraso entre paradas consecutivas (dados sintéticos).

Partida 0 do fixture ``programacao``: 24/11/2026 09:00 em São Paulo (12:00 UTC).
Percurso A → B → C → D, trajeto T1, sentido 0.
"""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from app import db
from app.core.tempo import FUSO
from app.indicadores.propagacao import UNIDADE, VERSAO_CALCULO, calcular_propagacao
from app.models import (
    ExecucaoColeta, ObservacaoTratada, Parada, PartidaProgramada, PassagemEstimada,
    RegistroBrutoGPS, Trecho, ViagemObservada,
)
from app.viagens.passagens import estimar_e_gravar

LAT = -23.55
PARADAS = {"A": -46.700, "B": -46.697, "C": -46.694, "D": -46.690}
PREVISTOS = {"A": "09:00:00", "B": "09:04:00", "C": "09:08:00", "D": "09:12:00"}


def _gtfs_para_delta(valor):
    h, m, s = map(int, valor.split(":"))
    return timedelta(hours=h, minutes=m, seconds=s)


@pytest.fixture
def cenario(programacao):
    versao, linha, importacao, partidas = programacao
    db.session.add_all(Parada(versao_gtfs_id=versao.id, codigo=c, nome=c, latitude=Decimal(str(LAT)),
                              longitude=Decimal(str(lon))) for c, lon in PARADAS.items())
    codigos = list(PARADAS)
    trechos = [Trecho(versao_gtfs_id=versao.id, linha_codigo=linha.codigo, sentido="0", trajeto="T1",
                      ordem=i + 1, parada_origem_codigo=o, parada_destino_codigo=d,
                      geometria=[[PARADAS[o], LAT], [PARADAS[d], LAT]])
               for i, (o, d) in enumerate(zip(codigos, codigos[1:]))]
    viagem = ViagemObservada(partida_programada_id=partidas[0].id, saida_observada=partidas[0].saida_prevista,
                             cobertura_tracado=1, versao_reconstrucao="recon-sint",
                             versao_correspondencia="match-sint")
    db.session.add_all(trechos + [viagem])
    db.session.commit()
    return {"versao": versao, "importacao": importacao, "partidas": partidas,
            "trechos": trechos, "viagem": viagem}


def passagens(cenario, atrasos, viagem=None, previstos=PREVISTOS, **extra):
    """``atrasos``: {parada: segundos} → PassagemEstimada em memória (horário previsto + atraso)."""
    viagem = viagem or cenario["viagem"]
    meia_noite = cenario["partidas"][0].saida_prevista.astimezone(FUSO).replace(hour=0, minute=0)
    campos = dict(viagem_observada_id=viagem.id, versao_gtfs_id=cenario["versao"].id,
                  metodo="INTERPOLACAO_LINEAR_PERCURSO", versao_estimativa="us15-t01-v1-lacuna120s",
                  posicao_anterior_id=1, posicao_posterior_id=2)
    campos.update(extra)
    return [PassagemEstimada(id=f"p-{c}", parada_codigo=c,
                             instante=meia_noite + _gtfs_para_delta(previstos[c]) + timedelta(seconds=s),
                             **campos)
            for c, s in atrasos.items()]


def calcular(cenario, atrasos, previstos=PREVISTOS, **kw):
    return calcular_propagacao(cenario["viagem"], cenario["trechos"], passagens(cenario, atrasos, **kw), previstos)


def por_trecho(resultado):
    return {(t.parada_origem, t.parada_destino): t for t in resultado.trechos}


# --- convenção de sinais --------------------------------------------------------

def test_exemplo_obrigatorio_aumento_de_120_para_300_segundos(cenario):
    t = por_trecho(calcular(cenario, {"A": 120, "B": 300}))[("A", "B")]
    assert (t.atraso_origem_seg, t.atraso_destino_seg, t.variacao_seg) == (120, 300, 180)
    assert t.motivo is None
    assert (t.passagem_origem_id, t.passagem_destino_id) == ("p-A", "p-B")


def test_reducao_de_atraso_e_negativa(cenario):
    t = por_trecho(calcular(cenario, {"A": 300, "B": 120}))[("A", "B")]
    assert t.variacao_seg == -180


def test_atraso_constante_e_zero(cenario):
    t = por_trecho(calcular(cenario, {"A": 120, "B": 120}))[("A", "B")]
    assert t.variacao_seg == 0


def test_adiantamento_conta_como_atraso_negativo(cenario):
    t = por_trecho(calcular(cenario, {"A": -60, "B": 60}))[("A", "B")]
    assert (t.atraso_origem_seg, t.variacao_seg) == (-60, 120)


def test_nao_usa_a_diferenca_entre_horarios_de_passagem(cenario):
    resultado = calcular(cenario, {"A": 120, "B": 300})
    p = {x.parada_codigo: x for x in passagens(cenario, {"A": 120, "B": 300})}
    diferenca_passagens = (p["B"].instante - p["A"].instante).total_seconds()  # 240 + 180
    assert diferenca_passagens == 420
    assert por_trecho(resultado)[("A", "B")].variacao_seg == 180


def test_todos_os_trechos_em_ordem_do_percurso(cenario):
    resultado = calcular(cenario, {"A": 0, "B": 60, "C": 30, "D": 30})
    assert [(t.ordem, t.variacao_seg) for t in resultado.trechos] == [(1, 60), (2, -30), (3, 0)]


# --- horários após meia-noite ----------------------------------------------------

def test_partida_apos_meia_noite_usa_a_data_operacional(cenario):
    # Partida de 24/11 às 24:02 (00:02 de 25/11 em São Paulo).
    importacao = cenario["importacao"]
    saida = cenario["partidas"][0].saida_prevista + timedelta(hours=15, minutes=2)
    partida = PartidaProgramada(importacao=importacao, ordem=9, saida_prevista=saida, tipo_oferta="FIXA")
    viagem = ViagemObservada(partida_programada=partida, saida_observada=saida, cobertura_tracado=1,
                             versao_reconstrucao="recon-sint", versao_correspondencia="match-sint")
    db.session.add_all([partida, viagem])
    db.session.commit()
    previstos = {"A": "24:02:00", "B": "24:06:00", "C": "24:10:00", "D": "24:14:00"}
    resultado = calcular_propagacao(viagem, cenario["trechos"],
                                    passagens(cenario, {"A": 120, "B": 300}, viagem=viagem,
                                              previstos=previstos), previstos)
    t = por_trecho(resultado)[("A", "B")]
    assert (t.atraso_origem_seg, t.variacao_seg) == (120, 180)
    assert resultado.data_operacao == importacao.data_operacao


def test_horario_previsto_de_outra_partida_ou_data_e_rejeitado(cenario):
    # Primeira parada às 09:15 = partida seguinte; ou 33:00 = 09:00 do dia seguinte.
    for primeira in ("09:15:00", "33:00:00"):
        with pytest.raises(ValueError, match="saída da partida"):
            calcular(cenario, {"A": 0}, previstos={**PREVISTOS, "A": primeira})


# --- ausências: nada inventado ------------------------------------------------------

def test_sem_horario_programado_fica_sem_valor(cenario):
    previstos = {k: v for k, v in PREVISTOS.items() if k != "C"}
    resultado = por_trecho(calcular(cenario, {"A": 0, "B": 60}, previstos=previstos))
    assert resultado[("A", "B")].variacao_seg == 60
    for chave in (("B", "C"), ("C", "D")):
        assert resultado[chave].variacao_seg is None
        assert resultado[chave].motivo == "horario_previsto_ausente"


def test_sem_nenhum_horario_programado(cenario):
    resultado = calcular_propagacao(cenario["viagem"], cenario["trechos"],
                                    passagens(cenario, {"A": 0, "B": 60}), {})
    assert all(t.variacao_seg is None and t.atraso_origem_seg is None
               and t.motivo == "horario_previsto_ausente" for t in resultado.trechos)


def test_passagem_ausente_fica_sem_valor(cenario):
    t = por_trecho(calcular(cenario, {"A": 0, "B": 60}))[("B", "C")]
    assert (t.atraso_origem_seg, t.atraso_destino_seg, t.variacao_seg) == (60, None, None)
    assert t.motivo == "passagem_ausente"


def test_oferta_por_frequencia_nao_recebe_atraso(cenario):
    cenario["partidas"][0].tipo_oferta, cenario["partidas"][0].intervalo_seg = "FREQUENCIA", 600
    resultado = calcular(cenario, {"A": 120, "B": 300})
    assert all(t.variacao_seg is None and t.atraso_origem_seg is None
               and t.motivo == "oferta_por_frequencia" for t in resultado.trechos)


def test_passagem_sem_rastreabilidade_nao_conta(cenario):
    lista = passagens(cenario, {"A": 120, "B": 300})
    lista[1] = replace_passagem(lista[1], posicao_posterior_id=None)
    t = calcular_propagacao(cenario["viagem"], cenario["trechos"], lista, PREVISTOS).trechos[0]
    assert (t.variacao_seg, t.motivo) == (None, "passagem_sem_rastreabilidade")


def replace_passagem(p, **mudancas):
    campos = {c: getattr(p, c) for c in ("id", "viagem_observada_id", "versao_gtfs_id", "parada_codigo",
                                         "instante", "metodo", "versao_estimativa",
                                         "posicao_anterior_id", "posicao_posterior_id")}
    return PassagemEstimada(**{**campos, **mudancas})


# --- pareamento incompatível: erro explícito ------------------------------------------

def test_paradas_nao_consecutivas_sao_rejeitadas(cenario):
    sem_meio = [t for t in cenario["trechos"] if t.ordem != 2]
    with pytest.raises(ValueError, match="consecutivas"):
        calcular_propagacao(cenario["viagem"], sem_meio, [], PREVISTOS)


def test_passagem_de_outra_viagem_e_rejeitada(cenario):
    outra = ViagemObservada(partida_programada_id=cenario["partidas"][1].id,
                            saida_observada=cenario["partidas"][1].saida_prevista, cobertura_tracado=1,
                            versao_reconstrucao="recon-sint", versao_correspondencia="match-sint")
    db.session.add(outra)
    db.session.commit()
    mistura = passagens(cenario, {"A": 0}) + passagens(cenario, {"B": 60}, viagem=outra)
    with pytest.raises(ValueError, match="outra viagem"):
        calcular_propagacao(cenario["viagem"], cenario["trechos"], mistura, PREVISTOS)


def test_sentido_incompativel_e_rejeitado(cenario):
    for t in cenario["trechos"]:
        t.sentido = "1"
    with pytest.raises(ValueError, match="sentido"):
        calcular(cenario, {"A": 0})


def test_versao_gtfs_diferente_e_rejeitada(cenario):
    with pytest.raises(ValueError, match="versão GTFS"):
        calcular(cenario, {"A": 0, "B": 60}, versao_gtfs_id="outra-versao")
    trechos = [replace_trecho(t, versao_gtfs_id="outra-versao") for t in cenario["trechos"]]
    with pytest.raises(ValueError, match="versão GTFS"):
        calcular_propagacao(cenario["viagem"], trechos, [], PREVISTOS)


def replace_trecho(t, **mudancas):
    campos = {c: getattr(t, c) for c in ("id", "versao_gtfs_id", "linha_codigo", "sentido", "trajeto",
                                         "ordem", "parada_origem_codigo", "parada_destino_codigo")}
    return Trecho(**{**campos, **mudancas})


def test_versoes_de_estimativa_misturadas_sao_rejeitadas(cenario):
    mistura = passagens(cenario, {"A": 0}) + passagens(cenario, {"B": 60}, versao_estimativa="outra")
    with pytest.raises(ValueError, match="versões de estimativa"):
        calcular_propagacao(cenario["viagem"], cenario["trechos"], mistura, PREVISTOS)


def test_passagem_de_parada_fora_do_percurso_e_rejeitada(cenario):
    lista = [replace_passagem(p, parada_codigo="Z") for p in passagens(cenario, {"A": 0})]
    with pytest.raises(ValueError, match="fora do percurso"):
        calcular_propagacao(cenario["viagem"], cenario["trechos"], lista, PREVISTOS)


# --- referências e versões ----------------------------------------------------------

def test_preserva_referencias_versoes_e_unidade(cenario):
    r = calcular(cenario, {"A": 120, "B": 300})
    v, imp = cenario["viagem"], cenario["importacao"]
    assert (r.viagem_observada_id, r.partida_programada_id, r.importacao_id) == (
        v.id, cenario["partidas"][0].id, imp.id)
    assert (r.versao_gtfs_id, r.data_operacao) == (cenario["versao"].id, imp.data_operacao)
    assert (r.versao_reconstrucao, r.versao_correspondencia, r.versao_estimativa) == (
        "recon-sint", "match-sint", "us15-t01-v1-lacuna120s")
    assert (r.unidade, r.versao_calculo) == (UNIDADE, VERSAO_CALCULO) == ("s", "propagacao-atraso-1.0")
    assert [t.trecho_id for t in r.trechos] == [t.id for t in cenario["trechos"]]


# --- integração com a estimativa real da US15-T01 ------------------------------------

def test_integra_com_passagens_gravadas_pela_us15_t01(cenario):
    """Posições sobre as paradas a cada 60 s; previsto a cada 30 s → atraso cresce 30 s por trecho."""
    t0, versao_id = cenario["viagem"].saida_observada, cenario["versao"].id
    execucao = ExecucaoColeta(iniciada_em=t0, estado="OK")
    db.session.add(execucao)
    obs = []
    for i, lon in enumerate(PARADAS.values()):
        bruto = RegistroBrutoGPS(execucao=execucao, prefixo_origem="5810", linha_origem="3762-10",
                                 latitude=LAT, longitude=lon, horario_gps=t0 + timedelta(seconds=60 * i))
        obs.append(ObservacaoTratada(bruto=bruto, valida=True, versao_correspondencia="match-sint",
                                     versao_gtfs_id=versao_id, linha_codigo="3762-10",
                                     sentido_gtfs="0"))
        db.session.add_all([bruto, obs[-1]])
    db.session.flush()
    brutos_antes = [(o.bruto.horario_gps, o.bruto.latitude, o.bruto.longitude) for o in obs]
    estimar_e_gravar(cenario["viagem"], obs, timedelta(seconds=120))
    db.session.commit()

    gravadas = PassagemEstimada.query.filter_by(viagem_observada_id=cenario["viagem"].id).all()
    previstos = {"A": "09:00:00", "B": "09:00:30", "C": "09:01:00", "D": "09:01:30"}
    r = calcular_propagacao(cenario["viagem"], Trecho.query.all(), gravadas, previstos)
    assert [(t.atraso_origem_seg, t.atraso_destino_seg, t.variacao_seg) for t in r.trechos] == [
        (0, 30, 30), (30, 60, 30), (60, 90, 30)]
    assert all(t.passagem_origem_id and t.passagem_destino_id for t in r.trechos)
    assert [(o.bruto.horario_gps, o.bruto.latitude, o.bruto.longitude) for o in obs] == brutos_antes


def test_motivo_separado_por_ponta_do_trecho(cenario):
    # C sem passagem e D sem horário previsto: cada ponta guarda o seu motivo.
    previstos = {k: v for k, v in PREVISTOS.items() if k != "D"}
    t = por_trecho(calcular(cenario, {"A": 0, "B": 60, "D": 90}, previstos=previstos))[("C", "D")]
    assert (t.motivo_origem, t.motivo_destino, t.motivo) == (
        "passagem_ausente", "horario_previsto_ausente", "passagem_ausente")
