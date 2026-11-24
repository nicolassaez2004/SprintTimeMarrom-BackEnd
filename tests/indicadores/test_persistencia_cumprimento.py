from datetime import timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from app import db
from app.indicadores.cumprimento import calcular_e_publicar_cumprimento
from app.models import (
    ImportacaoProgramacao,
    IndicadorOperacional,
    Parada,
    PartidaProgramada,
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
    assert indicador.versao_calculo == "cumprimento-1.1"
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


def _viagem(partida, segundos=0, cobertura=1.0):
    return ViagemObservada(
        partida_programada_id=partida.id,
        saida_observada=partida.saida_prevista + timedelta(seconds=segundos),
        cobertura_tracado=cobertura, versao_reconstrucao="recon-v1",
        versao_correspondencia="match-v3", qualidade={},
    )


def _publicar(versao, linha, inicio, fim):
    return calcular_e_publicar_cumprimento(
        linha=linha.codigo, sentido="0", inicio=inicio, fim=fim, versao_gtfs_id=versao.id,
        versao_correspondencia="match-v3", versao_reconstrucao="recon-v1",
    )


def test_tolerancia_independe_da_janela_de_15_minutos(app, programacao):
    """Grade 12:00/12:15/12:30: intervalo 15 min, tolerância ±7,5 min em qualquer janela."""
    versao, linha, _, partidas = programacao
    db.session.add_all([_viagem(partidas[1], 450), _viagem(partidas[2], -451)])
    db.session.commit()
    janela = _publicar(versao, linha, AGORA + timedelta(minutes=15), AGORA + timedelta(minutes=30))
    assert (janela.contagem_numerador, janela.contagem_denominador, janela.valor) == (1, 1, 100.0)
    ultima = _publicar(versao, linha, AGORA + timedelta(minutes=30), AGORA + timedelta(minutes=45))
    assert (ultima.contagem_numerador, ultima.contagem_denominador) == (0, 1)
    hora = _publicar(versao, linha, AGORA, AGORA + timedelta(hours=1))
    assert (hora.contagem_numerador, hora.contagem_denominador) == (1, 3)
    # Agregar = somar contagens das janelas, não média de percentuais.
    soma = sum(r.contagem_numerador for r in (janela, ultima)), sum(
        r.contagem_denominador for r in (janela, ultima))
    assert soma == (1, 2)


def test_captura_oficial_vazia_prevalece_sobre_gtfs(app, programacao):
    versao, linha, importacao, _ = programacao
    importacao.fonte = "GTFS"
    db.session.add(ImportacaoProgramacao(
        versao_gtfs_id=versao.id, linha=linha, data_operacao=importacao.data_operacao,
        sentido="0", tipo_dia="UTIL", fonte="OFICIAL", versao_fonte="oficial-vazia",
        fonte_url="https://example.invalid/oficial", capturada_em=AGORA,
        verificada_em=AGORA - timedelta(days=1), arquivo=importacao.arquivo,
    ))
    db.session.commit()
    registro = _publicar(versao, linha, AGORA, AGORA + timedelta(hours=1))
    assert (registro.contagem_denominador, registro.valor) == (0, None)
    assert [c["fonte"] for c in registro.qualidade["capturas_programacao"]] == ["OFICIAL"]


def test_dois_tipos_de_dia_na_mesma_data_nao_sao_somados(app, programacao):
    versao, linha, importacao, _ = programacao
    feriado = ImportacaoProgramacao(
        versao_gtfs_id=versao.id, linha=linha, data_operacao=importacao.data_operacao,
        sentido="0", tipo_dia="FERIADO", fonte="OFICIAL", versao_fonte="feriado",
        fonte_url="https://example.invalid/feriado", capturada_em=AGORA,
        verificada_em=AGORA, arquivo=importacao.arquivo,
    )
    feriado.partidas = [PartidaProgramada(ordem=1, saida_prevista=AGORA, tipo_oferta="FIXA")]
    db.session.add(feriado)
    db.session.commit()
    with pytest.raises(ValueError, match="programação ambígua"):
        _publicar(versao, linha, AGORA, AGORA + timedelta(hours=1))
    assert IndicadorOperacional.query.count() == 0


def test_versao_de_reconstrucao_diferente_nao_entra_no_calculo(app, programacao):
    versao, linha, _, partidas = programacao
    outra = _viagem(partidas[0])
    outra.versao_reconstrucao = "recon-v2"
    db.session.add(outra)
    db.session.commit()
    registro = _publicar(versao, linha, AGORA, AGORA + timedelta(hours=1))
    assert (registro.contagem_numerador, registro.contagem_denominador) == (0, 3)
    assert registro.qualidade["observacoes_viagem"] == 0


def test_api_filtros_combinados_e_borda_da_janela(app, programacao):
    versao, linha, _, partidas = programacao
    db.session.add(_viagem(partidas[0]))
    db.session.commit()
    for minuto in (0, 15, 30, 45):
        _publicar(versao, linha, AGORA + timedelta(minutes=minuto),
                  AGORA + timedelta(minutes=minuto + 15))
    cliente = app.test_client()
    base = (f"/indicadores?linha={linha.codigo}&sentido=0&tipo=CUMPRIMENTO_VIAGENS"
            f"&versao_gtfs_id={versao.id}")
    corpo = cliente.get(base + "&inicio=2026-11-24T12:00:00Z&fim=2026-11-24T12:30:00Z").json
    assert [d["inicio"] for d in corpo["dados"]] == [
        "2026-11-24T12:00:00+00:00", "2026-11-24T12:15:00+00:00"]
    assert corpo["dados"][0]["valor"] == 100.0
    assert corpo["dados"][0]["versao_calculo"] == "cumprimento-1.1"
    pontual = cliente.get(base + "&inicio=2026-11-24T12:15:00Z&fim=2026-11-24T12:15:00Z").json
    assert [d["inicio"] for d in pontual["dados"]] == ["2026-11-24T12:15:00+00:00"]
    vazio = cliente.get(base.replace("sentido=0", "sentido=1")).json
    assert vazio["paginacao"]["total"] == 0
    assert cliente.get(base.replace("CUMPRIMENTO", "PONTUALIDADE")).json["paginacao"]["total"] == 0
    # A janela sem partidas preserva NULL e contagens zero.
    ultima = cliente.get(base + "&inicio=2026-11-24T12:45:00Z&fim=2026-11-24T13:00:00Z").json
    assert ultima["dados"][0]["valor"] is None
    assert ultima["dados"][0]["contagens"] == {"numerador": 0, "denominador": 0}


@pytest.mark.parametrize("campos", [
    {"linha_codigo": "inexistente"},
    {"parada_codigo": "parada-inexistente"},
    {"trecho_id": "00000000-0000-0000-0000-00000000dead"},
    {"versao_gtfs_id": "00000000-0000-0000-0000-000000000000"},
])
def test_integridade_referencial_do_indicador(app, programacao, campos):
    versao, linha, _, _ = programacao
    db.session.add(IndicadorOperacional(**{
        "versao_gtfs_id": versao.id, "linha_codigo": linha.codigo, "sentido": "0",
        "indicador": "CUMPRIMENTO_VIAGENS", "valor": None, "unidade": "%",
        "periodo_inicio": AGORA, "periodo_fim": AGORA + timedelta(minutes=15),
        "versao_conjunto": "fk", "qualidade": {}, **campos,
    }))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


@pytest.mark.parametrize("campos,restricao", [
    ({"contagem_numerador": 2, "contagem_denominador": 1, "valor": 200.0}, "ck_indicador_contagens"),
    ({"contagem_numerador": 0, "contagem_denominador": 0, "valor": 0.0},
     "ck_indicador_denominador_zero_sem_valor"),
    ({"sentido": "2"}, "ck_indicador_sentido"),
    ({"periodo_fim": AGORA}, "ck_indicador_periodo"),
])
def test_restricoes_de_contagem_sentido_e_janela(app, programacao, campos, restricao):
    versao, linha, _, _ = programacao
    db.session.add(IndicadorOperacional(**{
        "versao_gtfs_id": versao.id, "linha_codigo": linha.codigo, "sentido": "0",
        "indicador": "CUMPRIMENTO_VIAGENS", "valor": None, "unidade": "%",
        "periodo_inicio": AGORA, "periodo_fim": AGORA + timedelta(minutes=15),
        "versao_conjunto": "ck", "qualidade": {}, **campos,
    }))
    with pytest.raises(IntegrityError, match=restricao):
        db.session.commit()
    db.session.rollback()
