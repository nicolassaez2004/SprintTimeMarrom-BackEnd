"""US20-T01A: janelas afetadas por GPS tardio, pelo horário do evento (revisão US20).

Cenário controlado (UTC; São Paulo = UTC-3): partidas às 12:00/12:15/12:30,
viagem 1 observada 12:01-12:40, posição tardia com evento 12:20 e recebimento
14:00, lacuna de passagem 120 s, próxima passagem existente às 12:52.
"""
from datetime import timedelta
from decimal import Decimal

import pytest

from app import db
from app.models import (
    ExecucaoColeta, ExecucaoProcessamento, IndicadorOperacional, ObservacaoTratada, Parada,
    PassagemEstimada, RegistroBrutoGPS, Trecho, ViagemObservada,
)
from app.reprocessamento.pedidos import registrar_pedidos_do_plano
from app.reprocessamento.planos import planejar_tardio
from app.reprocessamento.tardios import janelas_entre, localizar_janelas_tardias
from conftest import AGORA

M = lambda n: timedelta(minutes=n)  # noqa: E731
LACUNA = timedelta(seconds=120)
TIPOS = ("FREQUENCIA_OBSERVADA", "FREQUENCIA_PROGRAMADA", "REGULARIDADE_INTERVALOS",
         "ESPERA_ESTIMADA", "EXCESSO_ESPERA", "CUMPRIMENTO_VIAGENS", "PONTUALIDADE_VIAGENS")
V = dict(versao_base="base1", versao_regra="agenda", versao_codigo="git1")


@pytest.fixture
def cenario(programacao):
    versao, linha, _, partidas = programacao
    db.session.add_all(Parada(versao_gtfs_id=versao.id, codigo=c, nome=c, latitude=Decimal("-23.5"),
                              longitude=Decimal(lon)) for c, lon in (("P1", "-46.60"), ("P2", "-46.61")))
    coleta = ExecucaoColeta(estado="OK")
    apoio = [RegistroBrutoGPS(execucao=coleta, horario_gps=AGORA + M(50 + n), coletado_em=AGORA + M(50 + n))
             for n in range(2)]
    v1 = ViagemObservada(partida_programada_id=partidas[0].id, saida_observada=AGORA + M(1),
                         fim_observado=AGORA + M(40), cobertura_tracado=1,
                         versao_reconstrucao="r", versao_correspondencia="m")
    v3 = ViagemObservada(partida_programada_id=partidas[2].id, saida_observada=AGORA + M(31),
                         fim_observado=AGORA + M(70), cobertura_tracado=1,
                         versao_reconstrucao="r", versao_correspondencia="m")
    db.session.add_all(apoio + [v1, v3])
    db.session.flush()
    db.session.add(PassagemEstimada(viagem_observada_id=v3.id, versao_gtfs_id=versao.id,
        parada_codigo="P1", instante=AGORA + M(52), metodo="INTERPOLACAO", versao_estimativa="e",
        posicao_anterior_id=apoio[0].id, posicao_posterior_id=apoio[1].id))
    for sentido in ("0", "1"):
        for tipo in TIPOS:
            for minuto in (0, 15, 30, 45, 120):
                db.session.add(IndicadorOperacional(
                    chave_resultado=f"{tipo}-{minuto}-{sentido}", versao_gtfs_id=versao.id,
                    linha_codigo=linha.codigo, sentido=sentido, indicador=tipo, unidade="u",
                    periodo_inicio=AGORA + M(minuto), periodo_fim=AGORA + M(minuto + 15),
                    versao_conjunto="v1", versao_calculo="v1", publicado_em=AGORA + M(60)))
    db.session.commit()
    return {"versao": versao, "linha": linha, "partidas": partidas, "v1": v1, "coleta": coleta}


def tardio(cenario, evento, recebido=AGORA + M(120), sentido="0"):
    bruto = RegistroBrutoGPS(execucao=cenario["coleta"], horario_gps=evento, coletado_em=recebido,
                             payload={"p": "5810"})
    db.session.add(bruto)
    db.session.flush()
    db.session.add(ObservacaoTratada(bruto_id=bruto.id, valida=True, versao_correspondencia="m",
        versao_gtfs_id=cenario["versao"].id, linha_codigo=cenario["linha"].codigo, sentido_gtfs=sentido))
    db.session.commit()
    return bruto


def chaves(resultados):
    return {r.chave_resultado for r in resultados}


def test_usa_horario_do_evento_e_janela_vizinha_real(cenario):
    bruto = tardio(cenario, AGORA + M(20))
    impacto = localizar_janelas_tardias(bruto, lacuna=LACUNA)
    assert impacto["horario_evento"] == AGORA + M(20) and impacto["recebido_em"] == AGORA + M(120)
    assert chaves(impacto["resultados"]) == {
        "FREQUENCIA_OBSERVADA-15-0",
        "REGULARIDADE_INTERVALOS-15-0", "REGULARIDADE_INTERVALOS-45-0",  # 12:52 = vizinha
        "ESPERA_ESTIMADA-15-0", "ESPERA_ESTIMADA-45-0",
        "EXCESSO_ESPERA-15-0", "EXCESSO_ESPERA-45-0",                    # dependente da espera
        "CUMPRIMENTO_VIAGENS-0-0", "PONTUALIDADE_VIAGENS-0-0",           # partida da viagem 1
    }
    # A janela do recebimento (14:00), a programada, o outro sentido e a janela 12:30
    # (sem passagem afetada) ficam intactos.
    assert impacto["viagens"] == [cenario["v1"].id] and impacto["tardio"] is True
    assert impacto["abrangencia_partidas"] == "PARTIDAS_DAS_VIAGENS_QUE_COBREM_O_EVENTO"
    assert IndicadorOperacional.query.count() == 2 * len(TIPOS) * 5


def test_limite_de_janela_inclui_as_duas_janelas_tocadas(cenario):
    bruto = tardio(cenario, AGORA + M(14) + timedelta(seconds=30))
    impacto = localizar_janelas_tardias(bruto, lacuna=LACUNA)
    assert impacto["janelas"]["FREQUENCIA_OBSERVADA"] == [AGORA, AGORA + M(15)]
    assert janelas_entre(AGORA + M(15), AGORA + M(15)) == [AGORA + M(15)]  # início pertence à janela
    assert janelas_entre(AGORA + M(15) - timedelta(microseconds=1), AGORA + M(15)) == [AGORA, AGORA + M(15)]


def test_virada_de_dia_e_sem_viagem_usa_faixa_limitada(cenario):
    # 23:59:30 em São Paulo (02:59:30 UTC do dia seguinte), sem viagem que o cubra.
    evento = AGORA.replace(hour=2, minute=59, second=30) + timedelta(days=1)
    bruto = tardio(cenario, evento, recebido=evento + M(60))
    impacto = localizar_janelas_tardias(bruto, lacuna=LACUNA)
    assert impacto["janelas"]["FREQUENCIA_OBSERVADA"] == [evento.replace(minute=45, second=0),
                                                          evento.replace(hour=3, minute=0, second=0)]
    assert impacto["abrangencia_partidas"] == "SEM_VIAGEM_RECONSTRUIDA_FAIXA_TECNICA_24H"
    cumprimento = impacto["janelas"]["CUMPRIMENTO_VIAGENS"]
    # Janela que contém evento - 24 h até a que contém evento + lacuna; nada além disso.
    assert cumprimento[0] == evento.replace(minute=45, second=0) - timedelta(days=1)
    assert cumprimento[-1] == evento.replace(hour=3, minute=0, second=0)
    assert "CUMPRIMENTO_VIAGENS-0-0" in chaves(impacto["resultados"])  # 12:00 do dia anterior


def test_plano_repetido_e_idempotente_e_nao_altera_dados(cenario):
    bruto = tardio(cenario, AGORA + M(20))
    plano = planejar_tardio(bruto_id=bruto.id, lacuna=LACUNA, **V)
    db.session.commit()
    db.session.add(RegistroBrutoGPS(execucao=cenario["coleta"], horario_gps=AGORA + M(200),
                                    coletado_em=AGORA + M(200)))  # outro dado chega depois
    db.session.commit()
    assert planejar_tardio(bruto_id=bruto.id, lacuna=LACUNA, **V).id == plano.id
    detalhes = plano.parametros["detalhes"]
    assert detalhes["horario_evento"] == (AGORA + M(20)).isoformat()
    assert detalhes["recebido_em"] == (AGORA + M(120)).isoformat()
    assert detalhes["janelas_vizinhas"] == [(AGORA + M(45)).isoformat()]
    assert db.session.get(RegistroBrutoGPS, bruto.id).payload == {"p": "5810"}
    pedidos = registrar_pedidos_do_plano(plano)
    assert len(pedidos) == 9 and {p.parametros["origem"]["plano_id"] for p in pedidos} == {plano.id}
    regras = {p.parametros["indicador"]: p.versao_regra for p in pedidos}
    assert regras["CUMPRIMENTO_VIAGENS"] == "cumprimento-1.1" and regras["EXCESSO_ESPERA"] == "espera-1.0"
    assert [p.id for p in registrar_pedidos_do_plano(plano)] == [p.id for p in pedidos]


def test_chegada_antes_da_publicacao_nao_e_tardia(cenario):
    bruto = tardio(cenario, AGORA + M(20), recebido=AGORA + M(30))  # publicações às 13:00
    assert localizar_janelas_tardias(bruto, lacuna=LACUNA)["tardio"] is False
    with pytest.raises(ValueError, match="processamento regular"):
        planejar_tardio(bruto_id=bruto.id, lacuna=LACUNA, **V)
    assert ExecucaoProcessamento.query.count() == 0


def test_sem_correspondencia_valida_ou_sem_lacuna_definida(cenario, app):
    bruto = tardio(cenario, AGORA + M(20))
    ObservacaoTratada.query.filter_by(bruto_id=bruto.id).one().valida = False
    with pytest.raises(ValueError, match="correspondência"):
        localizar_janelas_tardias(bruto, lacuna=LACUNA)
    app.config["PASSAGEM_LACUNA_MAXIMA_SEG"] = None
    with pytest.raises(ValueError, match="lacuna"):
        localizar_janelas_tardias(tardio(cenario, AGORA + M(20)))


def test_variacao_e_agregado_mensal_ficam_sem_executor(cenario):
    versao, linha = cenario["versao"], cenario["linha"]
    trecho = Trecho(versao_gtfs_id=versao.id, linha_codigo=linha.codigo, sentido="0", trajeto="T",
                    ordem=1, parada_origem_codigo="P1", parada_destino_codigo="P2",
                    geometria=[[-46.60, -23.5], [-46.61, -23.5]])
    db.session.add(trecho)
    db.session.flush()
    for chave, tipo, inicio, fim, viagem in (
            ("var", "VARIACAO_ATRASO_TRECHO", AGORA - M(720), AGORA + M(720), cenario["v1"].id),
            ("var-outra", "VARIACAO_ATRASO_TRECHO", AGORA - M(720), AGORA + M(720), "outra"),
            ("mes", "AUMENTO_ATRASO_TRECHO_MENSAL", AGORA - timedelta(days=20), AGORA + timedelta(days=5), None)):
        db.session.add(IndicadorOperacional(chave_resultado=chave, versao_gtfs_id=versao.id,
            linha_codigo=linha.codigo, sentido="0", trecho_id=trecho.id, indicador=tipo, unidade="s",
            periodo_inicio=inicio, periodo_fim=fim, versao_conjunto="v", versao_calculo="v",
            publicado_em=AGORA + M(60), qualidade={"viagem_observada_id": viagem}))
    db.session.commit()
    plano = planejar_tardio(bruto_id=tardio(cenario, AGORA + M(20)).id, lacuna=LACUNA, **V)
    sem = {i["indicador"] for i in plano.parametros["resultados"] if not i["executavel"]}
    assert sem == {"VARIACAO_ATRASO_TRECHO", "AUMENTO_ATRASO_TRECHO_MENSAL"}
    assert len(plano.parametros["sem_executor"]) == 2  # "var-outra" é de outra viagem
    pedidos = registrar_pedidos_do_plano(plano)
    assert not {p.parametros["indicador"] for p in pedidos} & sem
