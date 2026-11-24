from datetime import timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from app import db
from app.indicadores.cumprimento import calcular_e_publicar_cumprimento
from app.models import (
    IndicadorOperacional,
    Parada,
    Trecho,
    ViagemObservada,
)
from tests.indicadores.conftest import AGORA


def test_calculo_persistido_guarda_contagens_versoes_e_e_idempotente(app, programacao):
    versao, linha, _, partidas = programacao
    db.session.add_all([
        ViagemObservada(
            partida_programada_id=partidas[0].id,
            saida_observada=partidas[0].saida_prevista + timedelta(minutes=7, seconds=30),
            cobertura_tracado=0.80,
            versao_reconstrucao="recon-v1", versao_correspondencia="match-v3",
            qualidade={"observacoes": 12},
        ),
        ViagemObservada(
            partida_programada_id=partidas[1].id,
            saida_observada=partidas[1].saida_prevista,
            cobertura_tracado=0.799,
            versao_reconstrucao="recon-v1", versao_correspondencia="match-v3",
            qualidade={"observacoes": 10},
        ),
    ])
    db.session.commit()

    argumentos = {
        "linha": linha.codigo, "sentido": "0", "inicio": AGORA,
        "fim": AGORA + timedelta(hours=1), "versao_gtfs_id": versao.id,
        "versao_correspondencia": "match-v3", "versao_reconstrucao": "recon-v1",
    }
    indicador = calcular_e_publicar_cumprimento(**argumentos)
    assert indicador.indicador == "CUMPRIMENTO_VIAGENS"
    assert indicador.valor == pytest.approx(33.33)
    assert (indicador.contagem_numerador, indicador.contagem_denominador) == (1, 3)
    assert indicador.versao_gtfs_id == versao.id
    assert indicador.versao_correspondencia == "match-v3"
    assert indicador.versao_calculo == "cumprimento-1.0"
    assert indicador.qualidade["origem_programacao"].startswith("OFICIAL")

    repetido = calcular_e_publicar_cumprimento(**argumentos)
    assert repetido.id == indicador.id
    assert IndicadorOperacional.query.count() == 1


def test_denominador_zero_grava_contagens_e_valor_null(app, programacao):
    versao, linha, _, _ = programacao
    registro = calcular_e_publicar_cumprimento(
        linha=linha.codigo, sentido="0", inicio=AGORA + timedelta(days=1),
        fim=AGORA + timedelta(days=2), versao_gtfs_id=versao.id,
        versao_correspondencia="match-v3", versao_reconstrucao="recon-v1",
    )
    assert registro.valor is None
    assert (registro.contagem_numerador, registro.contagem_denominador) == (0, 0)


def test_tabela_comum_aceita_dimensao_linha_parada_ou_trecho(app, programacao):
    versao, linha, _, _ = programacao
    origem = Parada(versao_gtfs_id=versao.id, codigo="s1", nome="Origem",
                    latitude=-23.5, longitude=-46.6)
    destino = Parada(versao_gtfs_id=versao.id, codigo="s2", nome="Destino",
                     latitude=-23.6, longitude=-46.7)
    db.session.add_all([origem, destino])
    db.session.flush()
    trecho = Trecho(
        versao_gtfs_id=versao.id, linha_codigo=linha.codigo, sentido="0",
        trajeto="trip-1", ordem=1, parada_origem_codigo=origem.codigo,
        parada_destino_codigo=destino.codigo,
        geometria=[[-46.6, -23.5], [-46.7, -23.6]],
    )
    db.session.add(trecho)
    db.session.flush()

    base = dict(
        versao_gtfs_id=versao.id, linha_codigo=linha.codigo, sentido="0",
        indicador="FREQUENCIA", valor=None, unidade="veiculos/h",
        periodo_inicio=AGORA, periodo_fim=AGORA + timedelta(hours=1),
        contagem_numerador=0, contagem_denominador=0, versao_conjunto="us14-test-v1",
        qualidade={"cobertura": 0.0},
    )
    por_linha = IndicadorOperacional(**base)
    por_parada = IndicadorOperacional(**base, parada_codigo=origem.codigo)
    por_trecho = IndicadorOperacional(**base, trecho_id=trecho.id)
    db.session.add_all([por_linha, por_parada, por_trecho])
    db.session.commit()
    assert IndicadorOperacional.query.count() == 3


def test_tabela_rejeita_parada_e_trecho_na_mesma_linha(app, programacao):
    versao, linha, _, _ = programacao
    origem = Parada(versao_gtfs_id=versao.id, codigo="s1", nome="Origem",
                    latitude=-23.5, longitude=-46.6)
    destino = Parada(versao_gtfs_id=versao.id, codigo="s2", nome="Destino",
                     latitude=-23.6, longitude=-46.7)
    db.session.add_all([origem, destino])
    db.session.flush()
    trecho = Trecho(
        versao_gtfs_id=versao.id, linha_codigo=linha.codigo, sentido="0",
        trajeto="trip-1", ordem=1, parada_origem_codigo=origem.codigo,
        parada_destino_codigo=destino.codigo,
        geometria=[[-46.6, -23.5], [-46.7, -23.6]],
    )
    db.session.add(trecho)
    db.session.commit()
    item = IndicadorOperacional(
        versao_gtfs_id=versao.id, linha_codigo=linha.codigo,
        parada_codigo=origem.codigo, trecho_id=trecho.id, sentido="0",
        indicador="FREQUENCIA", valor=None, unidade="veiculos/h",
        periodo_inicio=AGORA, periodo_fim=AGORA + timedelta(hours=1),
        contagem_numerador=0, contagem_denominador=0,
        versao_conjunto="us14-check-v1", qualidade={},
    )
    db.session.add(item)
    with pytest.raises(IntegrityError, match="ck_indicador_parada_ou_trecho"):
        db.session.commit()
    db.session.rollback()
