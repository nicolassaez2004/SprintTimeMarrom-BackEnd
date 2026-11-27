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


def test_plano_tardio_inclui_janela_vizinha_e_versoes(programacao):
    from app.models import ExecucaoColeta, ObservacaoTratada, RegistroBrutoGPS
    from app.reprocessamento.tardios import planejar_dado_tardio
    versao, linha, _, _ = programacao
    coleta = ExecucaoColeta(estado="OK")
    bruto = RegistroBrutoGPS(execucao=coleta, horario_gps=AGORA,
                             coletado_em=AGORA + timedelta(hours=1))
    db.session.add(bruto)
    db.session.flush()
    db.session.add(ObservacaoTratada(bruto_id=bruto.id, valida=True, versao_correspondencia="m1",
        versao_gtfs_id=versao.id, linha_codigo=linha.codigo, sentido_gtfs="0"))
    for n, sentido in [(0, "0"), (1, "0"), (2, "0"), (0, "1")]:
        inicio = AGORA + timedelta(minutes=15*n)
        db.session.add(IndicadorOperacional(chave_resultado=f"{n}-{sentido}",
            versao_gtfs_id=versao.id, linha_codigo=linha.codigo, sentido=sentido,
            indicador="REGULARIDADE_INTERVALOS", unidade="CV", periodo_inicio=inicio,
            periodo_fim=inicio + timedelta(minutes=15), versao_conjunto="v1", versao_calculo="v1"))
    db.session.commit()
    plano = planejar_dado_tardio(bruto, inicio_impacto=AGORA, fim_impacto=AGORA + timedelta(minutes=30),
        versao_base="base1", versao_regra="regra2", versao_codigo="git1",
        fechamento_em=AGORA + timedelta(minutes=15))
    assert {r.chave_resultado for r in plano["resultados"]} == {"0-0", "1-0"}
    assert (plano["versao_base"], plano["versao_regra"], plano["versao_codigo"]) == ("base1", "regra2", "git1")
    assert IndicadorOperacional.query.count() == 4


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
                      versao_base="base1", versao_regra="nova", versao_codigo="git1")
    plano = planejar_regra(**argumentos)
    assert plano.id == planejar_regra(**argumentos).id
    identificador = plano.id
    db.session.commit()
    db.session.expire_all()
    salvo = db.session.get(ExecucaoProcessamento, identificador)
    assert salvo.estado == "AGUARDANDO" and salvo.tipo == "PLANO_US20"
    assert salvo.parametros["resultados"][0]["id"]
    assert salvo.parametros["execucao_automatica_disponivel"] is False
    assert (salvo.versao_base, salvo.versao_regra, salvo.versao_codigo) == ("base1", "nova", "git1")


def test_plano_tardio_persistido_conserva_escopo(programacao):
    from app.models import ExecucaoColeta, ExecucaoProcessamento, ObservacaoTratada, RegistroBrutoGPS
    from app.reprocessamento.planos import planejar_tardio
    versao, linha, _, _ = programacao
    bruto = RegistroBrutoGPS(execucao=ExecucaoColeta(estado="OK"), horario_gps=AGORA,
                             coletado_em=AGORA + timedelta(hours=1))
    db.session.add(bruto)
    db.session.flush()
    db.session.add(ObservacaoTratada(bruto_id=bruto.id, valida=True, versao_correspondencia="m1",
        versao_gtfs_id=versao.id, linha_codigo=linha.codigo, sentido_gtfs="0"))
    for n, sentido in [(0, "0"), (1, "0"), (0, "1")]:
        inicio = AGORA + timedelta(minutes=15*n)
        db.session.add(IndicadorOperacional(chave_resultado=f"tardio-{n}-{sentido}",
            versao_gtfs_id=versao.id, linha_codigo=linha.codigo, sentido=sentido,
            indicador="REGULARIDADE_INTERVALOS", unidade="CV", periodo_inicio=inicio,
            periodo_fim=inicio + timedelta(minutes=15), versao_conjunto="v1",
            versao_calculo="v1", publicado_em=AGORA + timedelta(minutes=30)))
    db.session.commit()
    plano = planejar_tardio(bruto_id=bruto.id, versao_base="base1", versao_regra="regra2", versao_codigo="git1")
    identificador = plano.id
    db.session.commit()
    db.session.expire_all()
    salvo = db.session.get(ExecucaoProcessamento, identificador)
    assert len(salvo.parametros["resultados"]) == 2
    assert salvo.parametros["detalhes"]["abrangencia"] == "HISTORICO_INTEGRAL_DO_ESCOPO"
    assert IndicadorOperacional.query.count() == 3
