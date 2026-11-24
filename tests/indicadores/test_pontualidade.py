"""Cenários sintéticos da US16-T01; não comprovam pontualidade real da SPTrans."""

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app import create_app, db
from app.indicadores.pontualidade import SaidaObservada, calcular_diferencas
from app.models import (
    ArquivoArmazenado, ImportacaoProgramacao, Linha, PartidaProgramada, VersaoGTFS,
)
from app.repositories import programacao as repositorio
from app.viagens.separacao import ObservacaoViagem, SeparadorViagens, ViagemObservada

UTC = timezone.utc
SP = ZoneInfo("America/Sao_Paulo")
DIA = date(2026, 11, 24)
PREVISTA = datetime(2026, 11, 24, 8, tzinfo=SP)


def partida(id="p1", *, horario=PREVISTA, tipo="FIXA", **contexto):
    lote = ImportacaoProgramacao(
        id="importacao1", versao_gtfs_id="gtfs1", linha_codigo="8000-10", sentido="0",
        data_operacao=DIA, tipo_dia="UTIL", fonte="OFICIAL", versao_fonte="a" * 64,
        fonte_url="https://exemplo.invalid/programacao", capturada_em=PREVISTA,
        verificada_em=PREVISTA,
    )
    for chave, valor in contexto.items():
        setattr(lote, chave, valor)
    return PartidaProgramada(
        id=id, importacao=lote, ordem=1, saida_prevista=horario, tipo_oferta=tipo,
        intervalo_seg=600 if tipo == "FREQUENCIA" else None,
    )


def observada(id="v1", *, horario=PREVISTA, partida_id="p1", **contexto):
    viagem = ViagemObservada("5810", "8000-10", "0", PREVISTA,
                             PREVISTA + timedelta(hours=1), "mudanca_sentido")
    return SaidaObservada(id, viagem, contexto.get("data_operacao", DIA),
                         contexto.get("versao_gtfs_id", "gtfs1"), horario, partida_id)


@pytest.mark.parametrize("segundos", [-90, 0, 180, 0.125])
def test_sinal_observada_menos_programada_sem_arredondar(segundos):
    resultado = calcular_diferencas(
        [observada(horario=PREVISTA + timedelta(seconds=segundos))], [partida()],
    )
    assert resultado.viagens[0].status == "CALCULADA"
    assert resultado.viagens[0].diferenca_segundos == segundos
    assert resultado.denominador == 1


def test_compara_mesmo_instante_em_fusos_diferentes():
    entrada = observada(horario=PREVISTA.astimezone(UTC))
    item = calcular_diferencas([entrada], [partida()]).viagens[0]
    assert item.diferenca_segundos == 0
    assert item.saida_observada.tzinfo == item.programacao.saida_prevista.tzinfo == UTC


def test_normaliza_utc_antes_de_subtrair_em_repeticao_de_hora_local():
    # Os dois 01:30 são instantes distintos; subtrair no mesmo ZoneInfo dá zero.
    fuso = ZoneInfo("America/New_York")
    prevista = datetime(2026, 11, 1, 1, 30, tzinfo=fuso, fold=0)
    realizada = prevista.replace(fold=1)
    entrada = observada(horario=realizada, data_operacao=date(2026, 11, 1))
    entrada = replace(entrada, viagem=replace(entrada.viagem, inicio=prevista, fim=realizada))
    item = calcular_diferencas([entrada], [partida(
        horario=prevista, data_operacao=entrada.data_operacao,
    )]).viagens[0]
    assert item.diferenca_segundos == 3600


@pytest.mark.parametrize("prevista,realizada,delta", [
    (datetime(2026, 11, 25, 1, 10, tzinfo=SP), datetime(2026, 11, 25, 1, 12, tzinfo=SP), 120),
    (datetime(2026, 11, 25, 0, 1, tzinfo=SP), datetime(2026, 11, 24, 23, 59, tzinfo=SP), -120),
])
def test_horario_acima_de_24h_e_virada_preservam_dia_operacional(prevista, realizada, delta):
    item = calcular_diferencas([observada(horario=realizada)], [partida(horario=prevista)]).viagens[0]
    assert item.data_operacao == item.programacao.data_operacao == DIA
    assert item.diferenca_segundos == delta


@pytest.mark.parametrize("campo,valor", [
    ("linha_codigo", "8001-10"), ("sentido", "1"),
    ("data_operacao", DIA + timedelta(days=1)), ("versao_gtfs_id", "gtfs2"),
])
def test_pareamento_exige_mesma_linha_sentido_dia_e_versao(campo, valor):
    resultado = calcular_diferencas([observada()], [partida(**{campo: valor})])
    assert resultado.denominador == 0
    assert resultado.viagens[0].motivo == "CONTEXTO_DIVERGENTE"
    assert resultado.viagens[0].diferenca_segundos is None


@pytest.mark.parametrize("partida_id", [None, "inexistente"])
def test_nao_pareia_pela_proximidade_nem_substitui_id_desconhecido(partida_id):
    resultado = calcular_diferencas([observada(partida_id=partida_id)], [partida()])
    assert resultado.contagens["SEM_PAREAMENTO"] == 1
    assert resultado.denominador == 0
    assert resultado.viagens[0].programacao is None


def test_falta_de_programacao_nao_se_transforma_em_diferenca_zero():
    resultado = calcular_diferencas([observada()], [])
    assert resultado.viagens[0].status == "SEM_PAREAMENTO"
    assert resultado.viagens[0].diferenca_segundos is None


@pytest.mark.parametrize("partida_id", [None, "p1"])
def test_frequencia_nao_recebe_diferenca_mesmo_com_saida_e_pareamento(partida_id):
    entrada = observada(partida_id=partida_id, horario=PREVISTA + timedelta(minutes=40))
    resultado = calcular_diferencas([entrada], [partida(tipo="FREQUENCIA")])
    assert resultado.viagens[0].status == "FREQUENCIA"
    assert resultado.viagens[0].diferenca_segundos is None
    assert resultado.denominador == 0


def test_grade_mista_exige_pareamento_para_saber_tipo_da_oferta():
    fixa = partida()
    frequencia = partida("p2", tipo="FREQUENCIA")
    frequencia.importacao = fixa.importacao
    resultado = calcular_diferencas([observada(partida_id=None)], [fixa, frequencia])
    assert resultado.viagens[0].status == "SEM_PAREAMENTO"


def test_inicio_da_viagem_nao_e_usado_como_saida_confirmada():
    entrada = observada(horario=None)
    assert entrada.viagem.inicio == PREVISTA
    resultado = calcular_diferencas([entrada], [partida()])
    assert resultado.viagens[0].status == "SAIDA_NAO_CONFIRMADA"
    assert resultado.viagens[0].diferenca_segundos is None
    assert resultado.denominador == 0


def test_duas_viagens_na_mesma_partida_ficam_ambas_sem_pareamento():
    resultado = calcular_diferencas([observada(), observada("v2")], [partida()])
    assert resultado.denominador == 0
    assert resultado.contagens["SEM_PAREAMENTO"] == 2
    assert all(v.motivo == "PARTIDA_COMPARTILHADA" for v in resultado.viagens)


def test_denominador_exclui_frequencia_sem_pareamento_e_sem_saida_confirmada():
    partidas = [partida(), partida("p2", tipo="FREQUENCIA"), partida("p3")]
    for item in partidas[1:]:
        item.importacao = partidas[0].importacao
    entradas = [observada(), observada("v2", partida_id="p2"),
                observada("v3", partida_id=None), observada("v4", partida_id="p3", horario=None)]
    resultado = calcular_diferencas(iter(entradas), iter(partidas))
    assert resultado.denominador == 1
    assert resultado.contagens == {
        "CALCULADA": 1, "FREQUENCIA": 1, "SEM_PAREAMENTO": 1, "SAIDA_NAO_CONFIRMADA": 1,
    }
    assert sum(resultado.contagens.values()) == len(entradas)
    assert [v.viagem_id for v in resultado.viagens] == [v.viagem_id for v in entradas]


def test_conjunto_vazio_tem_denominador_zero_sem_percentual_inventado():
    resultado = calcular_diferencas([], [])
    assert resultado.viagens == ()
    assert resultado.denominador == sum(resultado.contagens.values()) == 0


@pytest.mark.parametrize("fonte", ["OFICIAL", "GTFS"])
def test_resultado_conserva_procedencia_e_identificadores(fonte):
    grade = partida(fonte=fonte)
    ref = calcular_diferencas([observada()], [grade]).viagens[0].programacao
    assert (ref.partida_id, ref.importacao_id, ref.versao_gtfs_id) == ("p1", "importacao1", "gtfs1")
    assert (ref.fonte, ref.versao_fonte, ref.fonte_url) == (
        fonte, "a" * 64, "https://exemplo.invalid/programacao",
    )


@pytest.mark.parametrize("campo", ["prevista", "observada", "inicio", "fim"])
def test_rejeita_instante_sem_fuso_sem_assumir_fuso_da_maquina(campo):
    entrada, grade = observada(), partida()
    naive = PREVISTA.replace(tzinfo=None)
    if campo == "prevista":
        grade.saida_prevista = naive
    elif campo == "observada":
        entrada = replace(entrada, saida_observada=naive)
    else:
        entrada = replace(entrada, viagem=replace(entrada.viagem, **{campo: naive}))
    with pytest.raises(ValueError, match="fuso explícito"):
        calcular_diferencas([entrada], [grade])


@pytest.mark.parametrize("valor", [PREVISTA, "2026-11-24", None])
def test_rejeita_data_operacional_inferida_ou_ausente(valor):
    with pytest.raises(ValueError, match="date explícita"):
        calcular_diferencas([observada(data_operacao=valor)], [partida()])


def test_rejeita_sentido_olho_vivo_e_viagem_com_intervalo_invertido():
    entrada = observada()
    with pytest.raises(ValueError, match="sentido_gtfs"):
        calcular_diferencas([replace(entrada, viagem=replace(entrada.viagem, sentido_gtfs="2"))], [])
    with pytest.raises(ValueError, match="anteceder"):
        calcular_diferencas([replace(entrada, viagem=replace(
            entrada.viagem, fim=entrada.viagem.inicio - timedelta(seconds=1),
        ))], [])


def test_duplicatas_de_entrada_nao_inflam_denominador():
    with pytest.raises(ValueError, match="viagem_id repetido"):
        calcular_diferencas([observada(), observada()], [partida()])
    with pytest.raises(ValueError, match="partida repetida"):
        calcular_diferencas([observada()], [partida(), partida()])


def test_nao_seleciona_silenciosamente_entre_capturas_da_mesma_programacao():
    outra = partida("p2")
    outra.importacao.id = "outra-importacao"
    with pytest.raises(ValueError, match="programação ambígua"):
        calcular_diferencas([observada()], [partida(), outra])


def test_integra_separacao_us14_e_programacao_us04_persistida_sem_gravar(tmp_path):
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://",
                      "MYSQL_SSL_CA": None})
    app.instance_path = str(tmp_path)
    with app.app_context():
        try:
            db.create_all()
            arquivo = ArquivoArmazenado(
                id="arquivo1", chave="gtfs/sintetico.zip", nome_original="sintetico.zip",
                mime_type="application/zip", tamanho_bytes=0, sha256="b" * 64,
            )
            versao = VersaoGTFS(id="gtfs1", hash="b" * 64, vigencia_inicio=DIA, arquivo=arquivo)
            linha = Linha(versao_gtfs=versao, codigo="8000-10", nome="Linha sintética")
            grade = partida(horario=PREVISTA.astimezone(UTC))
            grade.importacao.linha = linha
            grade.importacao.arquivo = arquivo
            db.session.add_all([grade, versao, linha])
            db.session.commit()
            db.session.expire_all()
            lote, tipos = repositorio.consultar_por_dia("gtfs1", "8000-10", "0", DIA, "UTIL")
            assert tipos == ["UTIL"]
            realizada = PREVISTA + timedelta(seconds=45)
            viagem = SeparadorViagens().separar([
                ObservacaoViagem("5810", "8000-10", "0", realizada, True),
                ObservacaoViagem("5810", "8000-10", "0", realizada + timedelta(minutes=5), True),
            ]).viagens[0]
            # Confirmação sintética explícita; o separador sozinho não a fornece.
            entrada = SaidaObservada("v1", viagem, DIA, versao.id, realizada, grade.id)
            resultado = calcular_diferencas([entrada], lote.partidas)
            assert resultado.viagens[0].diferenca_segundos == 45
            assert resultado.viagens[0].programacao.importacao_id == lote.id
            assert not db.session.new and not db.session.dirty and not db.session.deleted
            assert db.session.query(PartidaProgramada).count() == 1
            assert calcular_diferencas(
                [replace(entrada, saida_observada=None)], lote.partidas,
            ).denominador == 0
        finally:
            db.session.remove()
            db.engine.dispose()
