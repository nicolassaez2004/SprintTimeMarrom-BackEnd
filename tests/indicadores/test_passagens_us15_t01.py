"""US15-T01: passagem estimada entre duas posições válidas (dados sintéticos).

Percurso reto em latitude -23.55: A(-46.700) → B(-46.697) → C(-46.694) →
D(-46.690), cerca de 1 km. Posições sobre a reta, com horário relativo à
saída da partida 0 do fixture ``programacao``.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from app import db
from app.models import (
    ExecucaoColeta, ObservacaoTratada, Parada, PassagemEstimada, RegistroBrutoGPS,
    Trecho, ViagemObservada,
)
from app.viagens.passagens import (
    METODO, VERSAO, EstimadorPassagens, LimiteLacunaIndefinido, Percurso, estimar_e_gravar,
)

LAT = -23.55
PARADAS = {"A": -46.700, "B": -46.697, "C": -46.694, "D": -46.690}
LACUNA = timedelta(minutes=2)  # valor de teste; o oficial aguarda o PO


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
    return {"versao": versao, "linha": linha, "viagem": viagem, "execucao": execucao,
            "t0": partidas[0].saida_prevista}


def posicoes(cenario, *pontos, prefixo="5810"):
    """``pontos``: (segundos, longitude[, valida]) → ObservacaoTratada."""
    obs, versao_id, linha = [], cenario["versao"].id, cenario["linha"].codigo
    for segundos, lon, *resto in pontos:
        bruto = RegistroBrutoGPS(execucao=cenario["execucao"], prefixo_origem=prefixo,
                                 linha_origem="3762-10", latitude=LAT, longitude=lon,
                                 horario_gps=cenario["t0"] + timedelta(seconds=segundos))
        obs.append(ObservacaoTratada(
            bruto=bruto, valida=resto[0] if resto else True, versao_correspondencia="match-sint",
            versao_gtfs_id=versao_id, linha_codigo=linha, sentido_gtfs="0"))
        db.session.add_all([bruto, obs[-1]])
    db.session.flush()
    return obs


def estimar(cenario, obs, lacuna=LACUNA):
    percurso, _ = Percurso.da_rede(cenario["versao"].id, cenario["linha"].codigo, "0")
    return EstimadorPassagens(lacuna).estimar(percurso, obs)


def por_parada(resultado):
    return {p.parada_codigo: p for p in resultado.passagens}


def test_interpola_pelo_percurso_entre_duas_posicoes_validas(cenario):
    obs = posicoes(cenario, (0, -46.698), (60, -46.696))
    resultado = estimar(cenario, obs)
    passagem = por_parada(resultado)["B"]
    # B está no meio do par: ~30 s depois da primeira posição.
    assert abs((passagem.instante - cenario["t0"]).total_seconds() - 30) < 0.5
    assert (passagem.posicao_anterior_id, passagem.posicao_posterior_id) == (obs[0].bruto_id, obs[1].bruto_id)
    assert resultado.paradas_sem_evidencia == ("A", "C", "D")
    assert resultado.rejeicoes == ()


def test_parada_exatamente_sobre_a_posicao_recebe_o_horario_gps(cenario):
    obs = posicoes(cenario, (0, -46.699), (40, PARADAS["B"]), (80, -46.695))
    passagem = por_parada(estimar(cenario, obs))["B"]
    assert passagem.instante == cenario["t0"] + timedelta(seconds=40)
    # Conta no primeiro par que a alcança; o seguinte não a repete.
    assert passagem.posicao_posterior_id == obs[1].bruto_id


def test_ordem_cronologica_e_sentido_do_percurso(cenario):
    # Entregues fora de ordem: o estimador ordena pelo horário GPS.
    obs = posicoes(cenario, (120, -46.6895), (0, -46.7005), (60, -46.6955))
    resultado = estimar(cenario, obs)
    instantes = [p.instante for p in resultado.passagens]
    assert [p.parada_codigo for p in resultado.passagens] == ["A", "B", "C", "D"]
    assert instantes == sorted(instantes)
    assert all(cenario["t0"] <= i <= cenario["t0"] + timedelta(seconds=120) for i in instantes)


def test_movimento_contrario_nao_gera_passagem(cenario):
    obs = posicoes(cenario, (0, -46.693), (60, -46.698))
    resultado = estimar(cenario, obs)
    assert resultado.passagens == ()
    assert resultado.rejeicoes == ((obs[0].bruto_id, obs[1].bruto_id, "movimento_contrario"),)


def test_recuo_seguido_de_avanco_nao_repete_nem_recua_paradas(cenario):
    obs = posicoes(cenario, (0, -46.698), (60, -46.696), (90, -46.6985), (150, -46.6955))
    resultado = estimar(cenario, obs)
    assert list(por_parada(resultado)) == ["B"]
    assert por_parada(resultado)["B"].posicao_posterior_id == obs[1].bruto_id
    assert resultado.rejeicoes[0][2] == "movimento_contrario"


def test_posicao_invalida_interrompe_o_par(cenario):
    obs = posicoes(cenario, (0, -46.698), (30, -46.697, False), (60, -46.696))
    resultado = estimar(cenario, obs)
    assert resultado.passagens == ()
    assert {r[2] for r in resultado.rejeicoes} == {"posicao_invalida"}


def test_validacao_pendente_nao_serve_de_base(cenario):
    obs = posicoes(cenario, (0, -46.698), (60, -46.696, None), (90, -46.695))
    assert estimar(cenario, obs).passagens == ()


def test_lacuna_temporal_acima_do_limite(cenario):
    obs = posicoes(cenario, (0, -46.698), (121, -46.696))
    resultado = estimar(cenario, obs)
    assert resultado.passagens == ()
    assert resultado.rejeicoes[0][2] == "lacuna_temporal"
    # No limite exato o par é aceito.
    assert "B" in por_parada(estimar(cenario, obs, lacuna=timedelta(seconds=121)))


def test_limite_de_lacuna_nao_definido_impede_a_estimativa(cenario):
    with pytest.raises(LimiteLacunaIndefinido):
        EstimadorPassagens(None)
    # O app de teste não define PASSAGEM_LACUNA_MAXIMA_SEG.
    with pytest.raises(LimiteLacunaIndefinido):
        estimar_e_gravar(cenario["viagem"], [])


def test_parada_nao_atravessada(cenario):
    obs = posicoes(cenario, (0, -46.6995), (60, -46.6975))
    resultado = estimar(cenario, obs)
    assert "B" not in por_parada(resultado)
    assert "B" in resultado.paradas_sem_evidencia


def test_posicoes_insuficientes(cenario):
    resultado = estimar(cenario, posicoes(cenario, (0, PARADAS["B"])))
    assert resultado.passagens == ()
    assert resultado.motivo == "posicoes_insuficientes"
    assert resultado.paradas_sem_evidencia == tuple(PARADAS)


def test_observacoes_de_outro_veiculo_sao_recusadas(cenario):
    obs = posicoes(cenario, (0, -46.698)) + posicoes(cenario, (60, -46.696), prefixo="9999")
    with pytest.raises(ValueError):
        estimar(cenario, obs)


def test_trajeto_ambiguo_nao_e_escolhido_em_silencio(cenario):
    t = Trecho.query.filter_by(ordem=1).one()
    db.session.add(Trecho(versao_gtfs_id=t.versao_gtfs_id, linha_codigo=t.linha_codigo, sentido="0",
                          trajeto="T2", ordem=1, parada_origem_codigo="A",
                          parada_destino_codigo="B", geometria=t.geometria))
    db.session.flush()
    obs = posicoes(cenario, (0, -46.698), (60, -46.696))
    resultado = estimar_e_gravar(cenario["viagem"], obs, LACUNA)
    assert (resultado.passagens, resultado.motivo) == ((), "trajeto_ambiguo")
    assert PassagemEstimada.query.count() == 0


def test_grava_com_rastreabilidade_sem_duplicar_e_sem_alterar_brutos(cenario):
    obs = posicoes(cenario, (0, -46.6985), (60, -46.6955), (120, -46.6925))
    antes = [(b.id, b.latitude, b.longitude, b.horario_gps) for b in RegistroBrutoGPS.query]
    estimar_e_gravar(cenario["viagem"], obs, LACUNA)
    estimar_e_gravar(cenario["viagem"], obs, LACUNA)  # reexecução idempotente
    db.session.commit()

    gravadas = PassagemEstimada.query.order_by(PassagemEstimada.instante).all()
    assert [p.parada_codigo for p in gravadas] == ["B", "C"]
    b = gravadas[0]
    assert (b.metodo, b.versao_estimativa, b.versao_gtfs_id) == (METODO, VERSAO, cenario["versao"].id)
    assert b.viagem.id == cenario["viagem"].id and b.parada.nome == "Parada B"
    assert b.posicao_anterior.horario_gps <= b.instante <= b.posicao_posterior.horario_gps
    assert (b.posicao_anterior_id, b.posicao_posterior_id) == (obs[0].bruto_id, obs[1].bruto_id)
    assert [(r.id, r.latitude, r.longitude, r.horario_gps) for r in RegistroBrutoGPS.query] == antes


def test_banco_recusa_segunda_passagem_da_mesma_parada_e_viagem(cenario):
    obs = posicoes(cenario, (0, -46.698), (60, -46.696))
    estimar_e_gravar(cenario["viagem"], obs, LACUNA)
    db.session.commit()
    original = PassagemEstimada.query.one()
    db.session.add(PassagemEstimada(
        viagem_observada_id=original.viagem_observada_id, versao_gtfs_id=original.versao_gtfs_id,
        parada_codigo="B", instante=original.instante, metodo=METODO, versao_estimativa=VERSAO,
        posicao_anterior_id=original.posicao_anterior_id,
        posicao_posterior_id=original.posicao_posterior_id))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_observacao_fora_da_viagem_e_recusada(cenario):
    obs = posicoes(cenario, (-60, -46.699), (60, -46.696))
    with pytest.raises(ValueError):
        estimar_e_gravar(cenario["viagem"], obs, LACUNA)


def test_resultado_deterministico(cenario):
    obs = posicoes(cenario, (0, -46.7005), (50, -46.6965), (100, -46.6935), (150, -46.6895))
    assert estimar(cenario, obs) == estimar(cenario, list(reversed(obs)))
