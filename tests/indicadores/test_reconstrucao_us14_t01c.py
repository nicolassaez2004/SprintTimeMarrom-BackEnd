"""US14-T01C: reconstrução de ViagemObservada a partir das observações tratadas (dados SINTÉTICOS).

Rede do fixture ``programacao`` (linha 3762-10, sentido 0, 24/11/2026; partidas FIXAS 12:00, 12:15
e 12:30 UTC) com o percurso reto A(-46.700) → B(-46.697) → C(-46.694) → D(-46.690) em latitude
-23.55, cerca de 1,02 km. ``ORIGEM_M`` = 300 m: só posições antes de B (≈306 m) contam como origem.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app import db
from app.indicadores.cumprimento import calcular_indicador_cumprimento
from app.models import (
    ExecucaoColeta, ObservacaoTratada, Parada, PassagemEstimada, RegistroBrutoGPS, Trecho, ViagemObservada,
)
from app.viagens.reconstrucao import VERSAO_RECONSTRUCAO, cobertura, reconstruir, saida

# Mesmo instante do fixture ``programacao`` (tests/indicadores/conftest.py). Não usa
# ``from conftest import AGORA``: com outras pastas na mesma execução, o nome ``conftest`` pode
# apontar para outro arquivo (tests/programacao/conftest.py tem outro AGORA).
AGORA = datetime(2026, 11, 24, 12, tzinfo=timezone.utc)
LAT = -23.55
PARADAS = {"A": -46.700, "B": -46.697, "C": -46.694, "D": -46.690}
M = lambda n: timedelta(minutes=n)  # noqa: E731


@pytest.fixture
def rede(programacao, app):
    app.config["PASSAGEM_LACUNA_MAXIMA_SEG"] = "300"
    versao, linha, _, partidas = programacao
    db.session.add_all(Parada(versao_gtfs_id=versao.id, codigo=c, nome=c, latitude=Decimal(str(LAT)),
                              longitude=Decimal(str(lon))) for c, lon in PARADAS.items())
    codigos = list(PARADAS)
    db.session.add_all(Trecho(versao_gtfs_id=versao.id, linha_codigo=linha.codigo, sentido="0", trajeto="T1",
                              ordem=i + 1, parada_origem_codigo=o, parada_destino_codigo=d,
                              geometria=[[PARADAS[o], LAT], [PARADAS[d], LAT]])
                       for i, (o, d) in enumerate(zip(codigos, codigos[1:])))
    execucao = ExecucaoColeta(iniciada_em=AGORA, estado="OK")
    db.session.add(execucao)
    db.session.commit()
    return {"versao": versao, "linha": linha, "partidas": partidas, "execucao": execucao}


def percorrer(rede, prefixo, pontos):
    """``pontos``: (minutos desde 12:00, longitude) → bruto + observação tratada válida."""
    for minutos, lon in pontos:
        bruto = RegistroBrutoGPS(execucao=rede["execucao"], prefixo_origem=prefixo, linha_origem="33887",
                                 latitude=LAT, longitude=lon, horario_gps=AGORA + timedelta(minutes=minutos),
                                 coletado_em=AGORA + timedelta(minutes=minutos, seconds=20))
        db.session.add_all([bruto, ObservacaoTratada(
            bruto=bruto, valida=True, versao_correspondencia="match-1", versao_gtfs_id=rede["versao"].id,
            linha_codigo=rede["linha"].codigo, sentido_gtfs="0")])
    db.session.commit()


IDA_COMPLETA = [(-2, -46.7000), (-1, -46.6999), (0.5, -46.6998), (1.5, -46.6970), (3, -46.6940), (4.5, -46.6900)]


def viagens():
    db.session.expire_all()
    return ViagemObservada.query.order_by(ViagemObservada.saida_observada).all()


def test_funcoes_de_cobertura_e_saida():
    t = lambda m: AGORA + M(m)  # noqa: E731
    assert cobertura([(0, t(0)), (500, t(1)), (1000, t(2))], 1000) == 1.0
    assert cobertura([(0, t(0)), (500, t(1)), (1000, t(8))], 1000) == 0.5   # lacuna de 7 min não cobre
    assert cobertura([(0, t(0)), (600, t(1)), (300, t(2)), (900, t(3))], 1000) == 0.9  # recuo não conta
    assert saida([(10, t(0)), (50, t(1)), (400, t(2))]) == (t(1), "ULTIMA_POSICAO_NA_ORIGEM")
    assert saida([(500, t(0)), (900, t(1))]) == (t(0), "PRIMEIRA_POSICAO_FORA_DA_ORIGEM")
    assert saida([(10, t(0)), (20, t(1))]) is None  # ainda no terminal


def test_viagem_completa_associada_a_partida_com_passagens(rede):
    percorrer(rede, "5810", IDA_COMPLETA)
    resumo = reconstruir(AGORA - M(30), AGORA + M(60))
    [v] = viagens()
    assert v.partida_programada_id == rede["partidas"][0].id   # 12:00
    assert v.saida_observada == AGORA + timedelta(seconds=30)  # última posição na origem
    assert v.fim_observado == AGORA + timedelta(minutes=4.5)
    assert v.cobertura_tracado == pytest.approx(1.0, abs=0.02)
    assert (v.versao_reconstrucao, v.versao_correspondencia) == (VERSAO_RECONSTRUCAO, "match-1")
    assert v.qualidade["prefixo"] == "5810" and v.qualidade["saida"] == "ULTIMA_POSICAO_NA_ORIGEM"
    assert {p.parada_codigo for p in PassagemEstimada.query.filter_by(viagem_observada_id=v.id)} >= {"B", "C", "D"}
    assert resumo["viagens"] == 1


def test_viagem_iniciada_fora_da_origem_e_cobertura_parcial(rede):
    percorrer(rede, "5811", [(16, -46.6965), (17.5, -46.6940), (19, -46.6900)])  # começa depois de B
    reconstruir(AGORA, AGORA + M(60))
    [v] = viagens()
    assert v.partida_programada_id == rede["partidas"][1].id   # 12:15, a mais próxima de 12:16
    assert v.qualidade["saida"] == "PRIMEIRA_POSICAO_FORA_DA_ORIGEM"
    assert v.cobertura_tracado < 0.8                          # não conta como cumprida


def test_cumprimento_usa_a_viagem_reconstruida(rede):
    percorrer(rede, "5810", IDA_COMPLETA)
    reconstruir(AGORA - M(30), AGORA + M(60))
    campos = calcular_indicador_cumprimento(
        linha="3762-10", sentido="0", inicio=AGORA, fim=AGORA + M(15), versao_gtfs_id=rede["versao"].id,
        versao_correspondencia="match-1", versao_reconstrucao=VERSAO_RECONSTRUCAO)
    assert (campos["contagem_numerador"], campos["contagem_denominador"], campos["valor"]) == (1, 1, 100.0)


def test_dois_veiculos_disputam_a_mesma_partida_e_o_mais_proximo_fica_com_ela(rede):
    percorrer(rede, "A1", [(m + 3, lon) for m, lon in IDA_COMPLETA])   # saída 12:03:30
    percorrer(rede, "B2", [(m + 1, lon) for m, lon in IDA_COMPLETA])   # saída 12:01:30
    reconstruir(AGORA - M(30), AGORA + M(60))
    por_prefixo = {v.qualidade["prefixo"]: v.partida_programada_id for v in viagens()}
    assert por_prefixo["B2"] == rede["partidas"][0].id   # 12:00 fica com o mais próximo
    assert por_prefixo["A1"] == rede["partidas"][1].id   # o outro vai para 12:15
    assert len(set(por_prefixo.values())) == 2           # um a um


def test_repetir_nao_duplica_e_viagem_em_andamento_espera(rede):
    percorrer(rede, "5810", IDA_COMPLETA)
    percorrer(rede, "5812", [(30, -46.7000), (31, -46.6999), (32, -46.6960)])  # ainda rodando às 12:35
    primeiro = reconstruir(AGORA - M(30), AGORA + M(35))
    assert primeiro["viagens"] == 1 and primeiro["em_andamento"] == 1
    segundo = reconstruir(AGORA - M(30), AGORA + M(35))
    assert segundo.get("viagens", 0) == 0 and segundo["ja_reconstruidas"] == 1
    assert len(viagens()) == 1
    percorrer(rede, "5812", [(33.5, -46.6930), (35, -46.6900)])
    depois = reconstruir(AGORA - M(30), AGORA + M(90))      # mais tarde: a viagem terminou
    assert depois["viagens"] == 1 and len(viagens()) == 2


def test_veiculo_parado_no_terminal_nao_vira_viagem(rede):
    percorrer(rede, "5813", [(0, -46.7000), (2, -46.6999), (4, -46.7000), (6, -46.6998)])
    resumo = reconstruir(AGORA - M(30), AGORA + M(60))
    assert viagens() == [] and resumo["sem_deslocamento"] == 1


def test_sem_percurso_ou_sem_partida_nao_inventa_viagem(rede):
    percorrer(rede, "5814", [(m + 120, lon) for m, lon in IDA_COMPLETA])  # 14:00: nenhuma partida a até 60 min
    resumo = reconstruir(AGORA, AGORA + M(240))
    assert viagens() == [] and resumo["sem_partida_programada"] == 1


# --- Cadeia automática (tarefa periódica) ------------------------------------------------

def test_cadeia_reconstroi_sem_comando_manual_e_etapas_sao_independentes(rede, app, monkeypatch):
    from app.viagens import cadeia

    percorrer(rede, "5810", IDA_COMPLETA)
    app.config["MUNICIPIO_GEOJSON"] = None                    # sem limite: a correspondência espera
    monkeypatch.setattr(cadeia, "garantir_programacao", lambda dia: (_ for _ in ()).throw(RuntimeError("x")))
    resultado = cadeia.processar_cadeia(AGORA + M(90))
    assert resultado["programacao_hoje"]["situacao"] == "ERRO"           # registrada, sem derrubar o resto
    assert resultado["correspondencia"] == {"situacao": "SEM_LIMITE_MUNICIPAL", "processados": 0}
    assert resultado["reconstrucao"]["viagens"] == 1
    assert len(viagens()) == 1
    assert cadeia.processar_cadeia(AGORA + M(91))["reconstrucao"].get("viagens", 0) == 0  # idempotente
