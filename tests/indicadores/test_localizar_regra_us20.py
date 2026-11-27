from datetime import timedelta

import pytest

from app import db
from app.models import IndicadorOperacional
from app.reprocessamento.regras import localizar_uso_direto
from conftest import AGORA


def test_localiza_janelas_com_regra_alterada_sem_modificar_periodos(programacao):
    versao, linha, _, _ = programacao
    def indicador(chave, inicio, fim, regra):
        return IndicadorOperacional(chave_resultado=chave, versao_gtfs_id=versao.id,
            linha_codigo=linha.codigo, indicador="PONTUALIDADE_VIAGENS", unidade="%",
            periodo_inicio=inicio, periodo_fim=fim, versao_conjunto="v1", versao_calculo=regra)
    janela = timedelta(minutes=15)
    registros = [indicador("a", AGORA, AGORA + janela, "regra-1"),
                 indicador("b", AGORA + janela, AGORA + 2 * janela, "regra-1"),
                 indicador("c", AGORA, AGORA + janela, "regra-2")]
    db.session.add_all(registros)
    db.session.commit()
    encontrados = localizar_uso_direto("regra-1", AGORA + timedelta(minutes=5), AGORA + janela)
    assert [r.chave_resultado for r in encontrados] == ["a"]
    assert (encontrados[0].periodo_inicio, encontrados[0].periodo_fim) == (AGORA, AGORA + janela)
    assert IndicadorOperacional.query.count() == 3
    assert [r.versao_calculo for r in registros] == ["regra-1", "regra-1", "regra-2"]


def test_rejeita_intervalo_invalido(programacao):
    with pytest.raises(ValueError):
        localizar_uso_direto("regra-1", AGORA, AGORA)
    with pytest.raises(ValueError):
        localizar_uso_direto("regra-1", AGORA.replace(tzinfo=None), AGORA)
    with pytest.raises(ValueError):
        localizar_uso_direto(" ", AGORA, AGORA + timedelta(minutes=1))


def test_regra_inclui_excesso_dependente_e_preserva_outra_janela(programacao):
    from app.reprocessamento.regras import localizar_regra_e_dependentes
    versao, linha, _, _ = programacao
    for chave, tipo, inicio, regra in [
        ("espera", "ESPERA_ESTIMADA", AGORA, "espera-antiga"),
        ("excesso", "EXCESSO_ESPERA", AGORA, "outra-regra"),
        ("fora", "EXCESSO_ESPERA", AGORA + timedelta(minutes=15), "outra-regra"),
    ]:
        db.session.add(IndicadorOperacional(chave_resultado=chave, versao_gtfs_id=versao.id,
            linha_codigo=linha.codigo, sentido="0", indicador=tipo, unidade="min",
            periodo_inicio=inicio, periodo_fim=inicio + timedelta(minutes=15),
            versao_conjunto="v1", versao_calculo=regra))
    db.session.commit()
    encontrados = localizar_regra_e_dependentes("espera-antiga", AGORA, AGORA + timedelta(minutes=15))
    assert {r.chave_resultado for r in encontrados} == {"espera", "excesso"}
    assert IndicadorOperacional.query.count() == 3


def test_plano_regra_persistido_e_idempotente(programacao):
    from app.models import ExecucaoProcessamento
    from app.reprocessamento.planos import planejar_regra
    versao, linha, _, _ = programacao
    db.session.add(IndicadorOperacional(chave_resultado="plano", versao_gtfs_id=versao.id,
        linha_codigo=linha.codigo, sentido="0", indicador="ESPERA_ESTIMADA", unidade="min",
        periodo_inicio=AGORA, periodo_fim=AGORA + timedelta(minutes=15),
        versao_conjunto="v1", versao_calculo="antiga"))
    db.session.commit()
    argumentos = dict(regra_anterior="antiga", inicio=AGORA, fim=AGORA + timedelta(minutes=15),
                      versao_base="base1", versao_regra="espera-1.0", versao_codigo="git1")
    plano = planejar_regra(**argumentos)
    assert plano.id == planejar_regra(**argumentos).id
    identificador = plano.id
    db.session.commit()
    db.session.expire_all()
    salvo = db.session.get(ExecucaoProcessamento, identificador)
    assert salvo.estado == "AGUARDANDO" and salvo.tipo == "PLANO_US20"
    assert salvo.parametros["resultados"][0]["id"]
    assert salvo.parametros["execucao_automatica_disponivel"] is False
    assert (salvo.versao_base, salvo.versao_regra, salvo.versao_codigo) == ("base1", "espera-1.0", "git1")
    assert salvo.parametros["detalhes"]["regra_anterior"] == "antiga"
    assert salvo.parametros["detalhes"]["regra_nova"] == "espera-1.0"
    assert [i["regra"] for i in salvo.parametros["resultados"]] == ["espera-1.0"]


def test_regra_nova_nao_implementada_e_recusada_sem_gravar(programacao):
    from app.models import ExecucaoProcessamento
    from app.reprocessamento.planos import planejar_regra
    versao, linha, _, _ = programacao
    db.session.add(IndicadorOperacional(chave_resultado="x", versao_gtfs_id=versao.id,
        linha_codigo=linha.codigo, sentido="0", indicador="ESPERA_ESTIMADA", unidade="min",
        periodo_inicio=AGORA, periodo_fim=AGORA + timedelta(minutes=15),
        versao_conjunto="v1", versao_calculo="antiga"))
    db.session.commit()
    for nova in ("inexistente", "antiga"):
        with pytest.raises(ValueError):
            planejar_regra(regra_anterior="antiga", inicio=AGORA, fim=AGORA + timedelta(minutes=15),
                           versao_base="b", versao_regra=nova, versao_codigo="g")
    assert ExecucaoProcessamento.query.count() == 0
