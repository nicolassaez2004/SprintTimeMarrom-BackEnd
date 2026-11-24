"""US12-T04 com dados SINTÉTICOS (fev/2026, linha SINT-01): não são dados reais."""

from copy import deepcopy
import re
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import event

from app import db
from app.indicadores.cumprimento import calcular_e_publicar_cumprimento
from app.indicadores.pontualidade import calcular_e_publicar_pontualidade
from app.indicadores.reproducao import ReproducaoIndicadores
from test_frequencia_us15_t02 import MATCH as MATCH_US15, RECON as RECON_US15, T0 as T0_US15
from test_frequencia_us15_t02 import cenario, lacuna_de_teste, passagem  # noqa: F401 (fixtures)
from app.models import (
    ArquivoArmazenado, ExecucaoColeta, ImportacaoProgramacao, IndicadorOperacional,
    Linha, PartidaProgramada, RegistroBrutoGPS, VersaoGTFS, ViagemObservada,
)

UTC = timezone.utc
DIA = date(2026, 2, 10)
SAIDA = datetime(2026, 2, 10, 12, tzinfo=UTC)
JANELA = (SAIDA, SAIDA + timedelta(hours=1))
DEPOIS_DO_MES = datetime(2026, 3, 15, tzinfo=UTC)
VERSOES = {"versao_base": "alembic:sintetica", "versao_codigo": "git:sintetico",
           "versao_regra": "cumprimento-1.1+pontualidade-1.0+frequencia-1.0"}
CONTEXTO = {"linha": "SINT-01", "sentido": "0", "inicio": JANELA[0], "fim": JANELA[1],
            "versao_correspondencia": "match-sint-v1", "versao_reconstrucao": "recon-sint-v1"}


@pytest.fixture
def mes_sintetico(app):
    coleta = ExecucaoColeta(estado="OK", novos_persistidos=28)
    arquivo = ArquivoArmazenado(chave="gtfs/sintetico.zip", nome_original="sintetico.zip",
                                mime_type="application/zip", tamanho_bytes=0,
                                sha256="b" * 64, criado_em=SAIDA)
    versao = VersaoGTFS(hash="b" * 64, capturada_em=SAIDA, vigencia_inicio=date(2026, 1, 1),
                        vigencia_fim=date(2026, 12, 31), arquivo=arquivo)
    linha = Linha(versao_gtfs=versao, codigo="SINT-01", nome="Linha SINTÉTICA de teste")
    db.session.add_all([coleta, versao, linha])
    db.session.flush()
    # Um bruto por dia de fevereiro (09:00 em São Paulo).
    db.session.add_all(RegistroBrutoGPS(
        execucao_id=coleta.id, prefixo_origem=f"S{dia}", latitude=-23.5, longitude=-46.6,
        horario_gps=datetime(2026, 2, dia, 12, tzinfo=UTC),
        coletado_em=datetime(2026, 2, dia, 12, tzinfo=UTC), payload={"sintetico": dia},
    ) for dia in range(1, 29))
    importacao = ImportacaoProgramacao(
        versao_gtfs_id=versao.id, linha=linha, data_operacao=DIA, sentido="0",
        tipo_dia="UTIL", fonte="OFICIAL", versao_fonte="captura-sintetica",
        fonte_url="https://example.invalid/sintetico", capturada_em=SAIDA,
        verificada_em=SAIDA, arquivo=arquivo,
    )
    partidas = [PartidaProgramada(importacao=importacao, ordem=i + 1,
                                  saida_prevista=SAIDA + timedelta(minutes=15 * i),
                                  tipo_oferta="FIXA") for i in range(3)]
    db.session.add_all([importacao, *partidas])
    db.session.flush()
    # Partida 0: cumprida e pontual (+30 s); 1: cumprida e atrasada (+400 s); 2: sem viagem.
    db.session.add_all(ViagemObservada(
        partida_programada_id=partidas[i].id, cobertura_tracado=0.9,
        saida_observada=partidas[i].saida_prevista + timedelta(seconds=atraso),
        versao_reconstrucao="recon-sint-v1", versao_correspondencia="match-sint-v1",
    ) for i, atraso in ((0, 30), (1, 400)))
    db.session.commit()
    contexto = {**CONTEXTO, "versao_gtfs_id": versao.id}
    calcular_e_publicar_cumprimento(**contexto)
    calcular_e_publicar_pontualidade(**contexto)
    return contexto


def verificar(**opcoes):
    return ReproducaoIndicadores("2026-02", **{**VERSOES, "dados_sinteticos": True,
                                               "agora": DEPOIS_DO_MES, **opcoes}).executar()


def estado(modelo):
    return [{c.name: deepcopy(getattr(r, c.name)) for c in modelo.__table__.columns}
            for r in modelo.query.order_by(modelo.id)]


@pytest.fixture
def escritas():
    comandos = []

    def registrar(conn, cursor, sql, *args):
        if sql.lstrip().split()[0].upper() in ("INSERT", "UPDATE", "DELETE"):
            comandos.append(sql)

    event.listen(db.engine, "before_cursor_execute", registrar)
    yield comandos
    event.remove(db.engine, "before_cursor_execute", registrar)


def test_reproducao_deterministica_com_versoes_fixadas_e_sem_escritas(mes_sintetico, escritas):
    brutos, publicados = estado(RegistroBrutoGPS), estado(IndicadorOperacional)
    primeiro, segundo = verificar(), verificar()

    assert primeiro == segundo
    assert escritas == []
    assert primeiro["veredito"] == "REPRODUZIDO"
    assert primeiro["aceite_integral"] is False  # sintético + cadeia brutos→viagens ausente
    assert primeiro["origem_dados"] == "SINTETICA"
    assert primeiro["versoes"]["algoritmos"] == {
        "CUMPRIMENTO_VIAGENS": "cumprimento-1.1", "PONTUALIDADE_VIAGENS": "pontualidade-1.0",
        "FREQUENCIA_OBSERVADA": "frequencia-1.0", "FREQUENCIA_PROGRAMADA": "frequencia-1.0"}
    assert primeiro["tolerancia"]["modo"] == "IGUALDADE_EXATA"
    assert primeiro["completude"]["completo"] is True
    assert primeiro["preservacao"]["inalterado"] is True
    assert primeiro["preservacao"]["antes"]["brutos"] == 28
    assert (brutos, publicados) == (estado(RegistroBrutoGPS), estado(IndicadorOperacional))


def test_compara_indicadores_valores_contagens_e_unidades(mes_sintetico):
    relatorio = verificar()

    assert relatorio["quantidade_indicadores"] == 2
    assert relatorio["resultado"]["IGUAL"] == 2
    assert relatorio["por_indicador"] == {
        "CUMPRIMENTO_VIAGENS": {"publicados": 1, "reproduzidos": 1},
        "PONTUALIDADE_VIAGENS": {"publicados": 1, "reproduzidos": 1},
    }
    itens = {i["indicador"]: i for i in relatorio["itens"]}
    assert itens["CUMPRIMENTO_VIAGENS"]["contagens_recalculadas"] == [2, 3]
    assert itens["CUMPRIMENTO_VIAGENS"]["valor_recalculado"] == 66.67
    assert itens["PONTUALIDADE_VIAGENS"]["contagens_recalculadas"] == [1, 2]
    assert itens["PONTUALIDADE_VIAGENS"]["valor_recalculado"] == 50.0
    assert all(i["unidade"] == "%" and i["divergencias"] == [] for i in itens.values())


def test_detecta_divergencia_sem_corrigir_o_publicado(mes_sintetico):
    viagem = ViagemObservada.query.filter(ViagemObservada.saida_observada
                                          > SAIDA + timedelta(minutes=10)).one()
    viagem.cobertura_tracado = 0.5  # reconstrução mudou após a publicação
    db.session.commit()
    publicados = estado(IndicadorOperacional)

    relatorio = verificar()

    assert relatorio["veredito"] == "NAO_REPRODUZIDO"
    item = next(i for i in relatorio["itens"] if i["indicador"] == "CUMPRIMENTO_VIAGENS")
    assert item["status"] == "DIVERGENTE"
    campos = {d["campo"]: d for d in item["divergencias"]}
    assert campos["contagem_numerador"]["publicado"] == 2
    assert campos["contagem_numerador"]["recalculado"] == 1
    assert campos["valor"]["recalculado"] == 33.33
    assert estado(IndicadorOperacional) == publicados


def test_tolerancia_diferenciada_de_igualdade_exata(mes_sintetico):
    registro = IndicadorOperacional.query.filter_by(indicador="CUMPRIMENTO_VIAGENS").one()
    registro.valor = 66.674  # simula arredondamento distinto do publicado
    db.session.commit()

    exato = verificar()
    tolerante = verificar(tolerancia_valor=0.01)

    assert exato["resultado"]["DIVERGENTE"] == 1
    assert tolerante["resultado"]["IGUAL_COM_TOLERANCIA"] == 1
    assert tolerante["resultado"]["IGUAL"] == 1
    assert tolerante["tolerancia"]["modo"] == "ABSOLUTA"
    assert verificar(tolerancia_valor=0.001)["resultado"]["DIVERGENTE"] == 1


def test_null_e_dados_insuficientes(app, mes_sintetico):
    # Janela sem partidas: valor NULL publicado e recalculado como NULL.
    vazia = {**mes_sintetico, "inicio": JANELA[1], "fim": JANELA[1] + timedelta(hours=1)}
    calcular_e_publicar_pontualidade(**vazia)
    sem_versao = IndicadorOperacional.query.filter_by(indicador="CUMPRIMENTO_VIAGENS").one()
    sem_versao.qualidade = {k: v for k, v in sem_versao.qualidade.items()
                            if k != "versao_reconstrucao"}
    db.session.commit()

    relatorio = verificar()

    nulo = next(i for i in relatorio["itens"] if i["valor_publicado"] is None)
    assert (nulo["status"], nulo["valor_nulo"], nulo["valor_recalculado"]) == ("IGUAL", True, None)
    assert relatorio["resultado"]["DADOS_INSUFICIENTES"] == 1
    assert relatorio["veredito"] == "INCONCLUSIVO"
    reproducao = ReproducaoIndicadores("2026-02", **VERSOES, tolerancia_valor=1.0)
    assert reproducao._valor_igual(None, 0.0) == "DIVERGENTE"
    assert reproducao._valor_igual(None, None) == "IGUAL"


def test_identifica_versoes_incompativeis(mes_sintetico):
    antigo = IndicadorOperacional.query.filter_by(indicador="CUMPRIMENTO_VIAGENS").one()
    antigo.versao_calculo = "cumprimento-1.0"
    outro = IndicadorOperacional.query.filter_by(indicador="PONTUALIDADE_VIAGENS").one()
    outro.versao_correspondencia = "match-sint-v2"  # sem viagens nessa versão
    db.session.commit()

    relatorio = verificar()

    status = {i["indicador"]: i for i in relatorio["itens"]}
    assert status["CUMPRIMENTO_VIAGENS"]["status"] == "VERSAO_INCOMPATIVEL"
    assert status["PONTUALIDADE_VIAGENS"]["status"] == "DIVERGENTE"
    assert "chave_resultado" in {d["campo"] for d in status["PONTUALIDADE_VIAGENS"]["divergencias"]}
    assert relatorio["veredito"] == "NAO_REPRODUZIDO"


def test_mes_incompleto_nao_declara_reproducao(mes_sintetico):
    RegistroBrutoGPS.query.filter(RegistroBrutoGPS.prefixo_origem == "S5").delete()
    db.session.commit()

    sem_dia = verificar()
    em_curso = verificar(agora=datetime(2026, 2, 20, tzinfo=UTC))
    sem_publicacao = ReproducaoIndicadores("2026-01", **VERSOES, dados_sinteticos=True,
                                           agora=DEPOIS_DO_MES).executar()

    assert sem_dia["completude"]["dias_sem_brutos"] == ["2026-02-05"]
    assert sem_dia["veredito"] == "INCONCLUSIVO"
    assert "MES_NAO_ENCERRADO" in em_curso["completude"]["motivos"]
    assert "SEM_INDICADORES_PUBLICADOS" in sem_publicacao["completude"]["motivos"]
    assert sem_publicacao["quantidade_indicadores"] == 0
    assert not any(r["aceite_integral"] for r in (sem_dia, em_curso, sem_publicacao))


def test_reprocessamento_us12_t03_fixa_base_sem_alterar_brutos_nem_publicados(mes_sintetico, escritas):
    brutos, publicados = estado(RegistroBrutoGPS), estado(IndicadorOperacional)

    relatorio = verificar(reprocessar_brutos=True)
    repetido = verificar(reprocessar_brutos=True)

    reprocesso = relatorio["base"]["reprocessamento_us12_t03"]
    assert reprocesso["confere"] is True
    assert (reprocesso["tratados"], reprocesso["validos"], reprocesso["estado"]) == (28, 28, "PRONTO")
    assert repetido["base"] == relatorio["base"]  # mesmo manifesto e mesmas versões
    assert relatorio["veredito"] == "REPRODUZIDO"
    tabelas = {re.search(r"(?:INTO|UPDATE|FROM)\s+[\"`]?(\w+)", sql).group(1) for sql in escritas}
    assert tabelas == {"execucoes_processamento", "resultados_reprocessamento"}
    assert (brutos, publicados) == (estado(RegistroBrutoGPS), estado(IndicadorOperacional))


def test_base_sem_tabela_de_indicadores_e_inconclusiva(app):
    IndicadorOperacional.__table__.drop(db.engine)

    relatorio = verificar()

    assert relatorio["veredito"] == "INCONCLUSIVO"
    assert relatorio["completude"]["motivos"] == ["TABELA_AUSENTE:indicadores_operacionais"]
    assert relatorio["aceite_integral"] is False


def test_rejeita_regra_divergente_tolera_arredondamento_e_detecta_programacao_substituida(mes_sintetico):
    with pytest.raises(ValueError, match="versão de regra"):
        ReproducaoIndicadores("2026-02", **{**VERSOES, "versao_regra": "cumprimento-9.9"})
    assert ReproducaoIndicadores("2026-02", **VERSOES, tolerancia_valor=0.01)._valor_igual(
        66.68, 66.67) == "IGUAL_COM_TOLERANCIA"

    # Nova captura da mesma grade: a programação usada na publicação foi substituída.
    linha = Linha.query.one()
    antiga = ImportacaoProgramacao.query.one()
    nova = ImportacaoProgramacao(
        versao_gtfs_id=antiga.versao_gtfs_id, linha=linha, data_operacao=DIA, sentido="0",
        tipo_dia="UTIL", fonte="OFICIAL", versao_fonte="captura-2",
        fonte_url=antiga.fonte_url, capturada_em=SAIDA + timedelta(days=1),
        verificada_em=SAIDA + timedelta(days=1), arquivo=antiga.arquivo,
    )
    db.session.add_all([nova, *(PartidaProgramada(
        importacao=nova, ordem=i + 1, saida_prevista=SAIDA + timedelta(minutes=15 * i),
        tipo_oferta="FIXA") for i in range(3))])
    db.session.commit()
    substituida = verificar()
    assert {i["status"] for i in substituida["itens"]} == {"VERSAO_INCOMPATIVEL"}
    assert all("programação substituída" in i["detalhe"] for i in substituida["itens"])


def test_janela_na_virada_do_mes_deixa_relatorio_inconclusivo(mes_sintetico):
    # Janela na virada fev→mar não pode sumir em silêncio.
    db.session.add(IndicadorOperacional(
        versao_gtfs_id=mes_sintetico["versao_gtfs_id"], linha_codigo="SINT-01", sentido="0",
        indicador="CUMPRIMENTO_VIAGENS", unidade="%", valor=None,
        periodo_inicio=datetime(2026, 3, 1, 2, tzinfo=UTC),
        periodo_fim=datetime(2026, 3, 1, 4, tzinfo=UTC),
        contagem_numerador=0, contagem_denominador=0, versao_conjunto="virada",
    ))
    db.session.commit()
    virada = verificar()
    assert virada["veredito"] == "INCONCLUSIVO"
    assert any("virada do mês" in m for m in virada["completude"]["motivos"])



def test_reproduz_frequencia_publicada_da_us15(cenario, app):
    from app.indicadores.frequencia import calcular_e_publicar_frequencias

    app.config["GPS_COLLECTION_INTERVAL_SECONDS"] = 60
    passagem(cenario, "B", T0_US15 + timedelta(minutes=5))
    calcular_e_publicar_frequencias(
        linha="3762-10", sentido="0", data_operacao=date(2026, 11, 24),
        versao_gtfs_id=cenario["versao"].id, versao_correspondencia=MATCH_US15,
        versao_reconstrucao=RECON_US15, agora=datetime(2026, 11, 26, 12, tzinfo=UTC))
    relatorio = ReproducaoIndicadores("2026-11", **VERSOES, dados_sinteticos=True,
                                      agora=datetime(2026, 12, 2, tzinfo=UTC)).executar()
    frequencias = [i for i in relatorio["itens"] if i["indicador"].startswith("FREQUENCIA")]
    assert len(frequencias) == 96 * 5
    assert {i["status"] for i in frequencias} == {"IGUAL"}, [
        (i["status"], i.get("detalhe"), i.get("divergencias")) for i in frequencias if i["status"] != "IGUAL"][:1]
    assert relatorio["resultado"]["NAO_SUPORTADO"] == 0
