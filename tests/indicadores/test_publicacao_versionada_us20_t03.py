"""US20-T03: o recálculo publica uma versão consistente (dados SINTÉTICOS, SQLite em arquivo).

Linha 3762-10, partida 12:00 UTC de 24/11/2026. O conjunto antigo é publicado pela
agenda (US23-T03) com a viagem válida: CUMPRIMENTO_VIAGENS de 12:00–12:15 = 100%.
Depois a cobertura do traçado é corrigida para 0,5 (< 0,80): o recálculo deve
publicar 0% como nova versão.

Cada ``app_context`` tem sessão e conexão próprias (base em arquivo), então os
leitores da API não compartilham a transação do worker. A mesma garantia em
MySQL 8 depende do isolamento REPEATABLE READ/READ COMMITTED do InnoDB, que não
expõe linhas não confirmadas (o caminho de código é o mesmo).
"""
from datetime import date, timedelta
from threading import Event, Thread

import pytest

from app import db
from app.exportacoes.csv import (
    VersaoIndisponivel, arquivo_indicadores_completo, linhas_da_versao, publicar_csv_indicadores,
)
from app.indicadores.agenda import (
    ATUALIZACAO, CONSOLIDACAO, RECALCULO, atualizar_janela, consolidar_dia, versao_vigente,
)
from app.indicadores.cumprimento import VERSAO_ALGORITMO
from app.jobs.executor import Executor
from app.models import ExecucaoProcessamento, IndicadorOperacional, ViagemObservada
from app.reprocessamento import execucao as recalculo
from app.reprocessamento.pedidos import TIPO, registrar_pedidos
from conftest import AGORA
from test_retomada_recalculo_us20_t02c import _job, app, config  # noqa: F401 (fixtures)

LINHA = {"linha": "3762-10", "sentido": "ida"}
DIA = date(2026, 11, 24)
FILTROS = {"linha": None, "sentido": None, "tipo": None, "parada": None, "trecho": None,
           "versao_gtfs_id": None, "inicio": AGORA, "fim": AGORA + timedelta(minutes=15)}
DEPOIS = AGORA + timedelta(hours=2)


class _Livre:
    permitido, tentar_em = True, 0

    def cabecalhos(self):
        return {}


@pytest.fixture(autouse=True)
def sem_limitador(monkeypatch):
    """O limite por IP (US11-T02) grava a cada consulta; no SQLite isso disputaria o
    bloqueio do arquivo com o worker. Fora do escopo desta tarefa: liberado."""
    import app.api.comum as comum

    monkeypatch.setattr(comum, "consumir_limite", lambda _ip: _Livre())


@pytest.fixture
def antigo(app, programacao):
    """Conjunto antigo publicado pela agenda; a cobertura é corrigida em seguida."""
    versao, _, _, partidas = programacao
    viagem = ViagemObservada(partida_programada=partidas[0], cobertura_tracado=0.9,
                             saida_observada=partidas[0].saida_prevista + timedelta(seconds=30),
                             versao_reconstrucao="recon-1", versao_correspondencia="match-1")
    db.session.add(viagem)
    db.session.commit()
    atualizar_janela(AGORA + timedelta(minutes=16))  # janela 12:00–12:15
    publicada = versao_vigente()
    assert publicada["tipo"] == ATUALIZACAO and _cumprimento() == 100.0
    viagem.cobertura_tracado = 0.5  # correção: a viagem não cobre o traçado mínimo
    db.session.commit()
    return {"gtfs": versao.id, "versao": publicada["id"]}


def _pedir(gtfs):
    item = {"indicador": "CUMPRIMENTO_VIAGENS", "inicio": AGORA, "fim": AGORA + timedelta(minutes=15),
            "linha": "3762-10", "sentido": "0", "gtfs": gtfs}
    [pedido] = registrar_pedidos([item], versao_base="viagens_observadas", versao_regra=VERSAO_ALGORITMO,
                                 versao_codigo="git-us20-t03", origem={"tipo": "TESTE"})
    return pedido.id


def _cumprimento():
    db.session.expire_all()
    registro = IndicadorOperacional.query.filter_by(indicador="CUMPRIMENTO_VIAGENS",
                                                    periodo_inicio=AGORA).one()
    return registro.valor


def _ler(app):
    """O que o painel vê agora (outra conexão): (versao_dados.id, cumprimento 12:00).

    Um ``app_context`` novo dá sessão própria; sem ele a requisição reutilizaria a
    sessão do worker e veria a transação ainda aberta."""
    with app.app_context():
        try:
            corpo = app.test_client().get("/series", query_string=LINHA).get_json()
        finally:
            db.session.remove()
    [ponto] = [p for p in corpo["dados"]
               if p["tipo"] == "CUMPRIMENTO_VIAGENS" and p["inicio"] == AGORA.isoformat()]
    return corpo["versao_dados"]["id"], ponto["valor"]


def _executor(app, preservar=recalculo.preservar_publicacao, apos_confirmar=recalculo.invalidar_caches):
    return Executor(app, TIPO, recalculo.calcular_pedido, recalculo.publicar_pedido, preservar=preservar,
                    apos_confirmar=apos_confirmar, relogio=lambda: DEPOIS)


def _cumprimento_da_versao(versao_id):
    _, linhas = linhas_da_versao(versao_id, FILTROS)
    [linha] = [r for r in linhas if r["tipo"] == "CUMPRIMENTO_VIAGENS"]
    return float(linha["valor"])


def test_worker_usa_publicacao_versionada():
    executor = recalculo.executor_recalculo(object())
    assert (executor.tipo, executor.tarefa, executor.publicar) == (
        RECALCULO, recalculo.calcular_pedido, recalculo.publicar_pedido)
    assert executor.preservar is recalculo.preservar_publicacao
    assert executor.apos_confirmar is recalculo.invalidar_caches
    assert TIPO == RECALCULO


def test_leitor_durante_a_transacao_ve_o_antigo_e_depois_o_novo(app, antigo):
    pedido = _pedir(antigo["gtfs"])
    durante = []

    def preservar_e_ler(job_id):
        recalculo.preservar_publicacao(job_id)
        durante.append(_ler(app))  # resultados, PRONTO e arquivo gravados, sem commit

    assert _executor(app, preservar_e_ler).executar_um() == (pedido, "PRONTO")
    assert durante == [(antigo["versao"], 100.0)]
    assert _ler(app) == (pedido, 0.0)
    vigente = versao_vigente()
    assert (vigente["id"], vigente["tipo"]) == (pedido, RECALCULO)
    # Versões mantidas: a antiga continua reproduzível e a nova tem o seu arquivo.
    assert _cumprimento_da_versao(antigo["versao"]) == 100.0
    assert _cumprimento_da_versao(pedido) == 0.0
    job = _job(pedido)
    assert (job.versao_base, job.versao_regra, job.versao_codigo) == (
        "viagens_observadas", VERSAO_ALGORITMO, "git-us20-t03")
    registro = IndicadorOperacional.query.filter_by(indicador="CUMPRIMENTO_VIAGENS",
                                                    periodo_inicio=AGORA).one()
    assert registro.qualidade["recalculo"]["pedido_id"] == pedido


def test_rollback_mantem_o_conjunto_e_a_versao_antigos(app, antigo):
    pedido = _pedir(antigo["gtfs"])
    durante = []

    def preservar_ler_e_falhar(job_id):
        recalculo.preservar_publicacao(job_id)
        durante.append(_ler(app))
        raise OSError("disco cheio")

    assert _executor(app, preservar_ler_e_falhar).executar_um() == (pedido, "ERRO")
    assert durante == [(antigo["versao"], 100.0)]
    assert _ler(app) == (antigo["versao"], 100.0)
    assert _cumprimento() == 100.0  # nada parcial ficou gravado
    job = _job(pedido)
    assert (job.estado, job.erro) == ("ERRO", "OSError")
    with pytest.raises(VersaoIndisponivel) as erro:
        linhas_da_versao(pedido, FILTROS)
    assert erro.value.status == 404  # versão não publicada


def test_leitores_concorrentes_nunca_veem_mistura(app, antigo):
    """Leitores em outra thread durante todo o recálculo: só (antiga, 100) ou (nova, 0)."""
    pedido = _pedir(antigo["gtfs"])
    em_transacao, lido_durante, fim = Event(), Event(), Event()
    leituras, erros = [], []

    def ler_sem_parar():
        with app.app_context():
            try:
                while not fim.is_set():
                    leituras.append(_ler(app))
                    if em_transacao.is_set():
                        lido_durante.set()
                leituras.append(_ler(app))
            except Exception as erro:  # noqa: BLE001 (relatado no assert)
                erros.append(repr(erro))
                lido_durante.set()

    def preservar_e_esperar(job_id):
        recalculo.preservar_publicacao(job_id)
        em_transacao.set()
        assert lido_durante.wait(10)  # um leitor consultou com a transação aberta

    leitor = Thread(target=ler_sem_parar)
    leitor.start()
    try:
        assert _executor(app, preservar_e_esperar).executar_um() == (pedido, "PRONTO")
    finally:
        fim.set()
        leitor.join(30)
    assert erros == []
    assert set(leituras) <= {(antigo["versao"], 100.0), (pedido, 0.0)}
    assert leituras[0] == (antigo["versao"], 100.0) and leituras[-1] == (pedido, 0.0)
    # Sem voltar atrás: depois de ver a versão nova, nenhum leitor vê a antiga.
    primeira_nova = leituras.index((pedido, 0.0))
    assert set(leituras[primeira_nova:]) == {(pedido, 0.0)}


def _csv_do_dia():
    import csv as leitor_csv
    import io

    arquivo = arquivo_indicadores_completo(DIA)
    if arquivo is None:
        return None
    linhas = leitor_csv.DictReader(io.StringIO(arquivo["conteudo"].decode("utf-8-sig")))
    [linha] = [r for r in linhas if r["tipo"] == "CUMPRIMENTO_VIAGENS" and r["inicio"] == AGORA.isoformat()]
    return float(linha["valor"])


@pytest.fixture
def consolidado(app, programacao):
    """Dia 24/11 consolidado e CSV diário publicado com o valor antigo (100%)."""
    versao, _, _, partidas = programacao
    viagem = ViagemObservada(partida_programada=partidas[0], cobertura_tracado=0.9,
                             saida_observada=partidas[0].saida_prevista + timedelta(seconds=30),
                             versao_reconstrucao="recon-1", versao_correspondencia="match-1")
    db.session.add(viagem)
    db.session.commit()
    agora = AGORA + timedelta(hours=17)  # 25/11 05:00 em São Paulo
    assert consolidar_dia(DIA, agora).tipo == CONSOLIDACAO
    publicar_csv_indicadores(DIA, agora)
    assert _csv_do_dia() == 100.0
    viagem.cobertura_tracado = 0.5
    db.session.commit()
    return {"gtfs": versao.id, "agora": agora}


def test_csv_diario_vencido_nao_e_servido_e_e_regerado(app, consolidado):
    pedido = _pedir(consolidado["gtfs"])
    relogio = consolidado["agora"] + timedelta(hours=1)
    sem_cache = Executor(app, TIPO, recalculo.calcular_pedido, recalculo.publicar_pedido,
                         preservar=recalculo.preservar_publicacao, relogio=lambda: relogio)
    assert sem_cache.executar_um() == (pedido, "PRONTO")
    assert _csv_do_dia() is None  # o arquivo com 100% não é mais servido
    resposta = app.test_client().get("/downloads/indicadores",
                                     query_string={"formato": "csv", "data": DIA.isoformat()})
    assert resposta.status_code == 404
    assert recalculo.invalidar_caches(pedido) == [DIA]
    assert _csv_do_dia() == 0.0


def test_worker_completo_regera_o_csv_depois_do_commit(app, consolidado):
    pedido = _pedir(consolidado["gtfs"])
    relogio = consolidado["agora"] + timedelta(hours=1)
    executor = recalculo.executor_recalculo(app, relogio=lambda: relogio)
    assert executor.executar_um() == (pedido, "PRONTO")
    assert _csv_do_dia() == 0.0
    resposta = app.test_client().get("/downloads/indicadores",
                                     query_string={"formato": "csv", "data": DIA.isoformat()})
    assert resposta.status_code == 200


def test_falha_ao_regerar_cache_nao_desfaz_a_publicacao(app, consolidado):
    pedido = _pedir(consolidado["gtfs"])
    relogio = consolidado["agora"] + timedelta(hours=1)

    def cache_falha(job_id):
        raise OSError("sem espaço")

    executor = Executor(app, TIPO, recalculo.calcular_pedido, recalculo.publicar_pedido,
                        preservar=recalculo.preservar_publicacao, apos_confirmar=cache_falha,
                        relogio=lambda: relogio)
    assert executor.executar_um() == (pedido, "PRONTO")
    assert _job(pedido).estado == "PRONTO" and _cumprimento() == 0.0
    assert _csv_do_dia() is None  # vencido continua fora do ar até ser regerado


def test_dias_afetados_seguem_o_fuso_de_sao_paulo():
    pedido = ExecucaoProcessamento(periodo_inicio=AGORA + timedelta(hours=14, minutes=45),
                                   periodo_fim=AGORA + timedelta(hours=15))  # 23:45–00:00 local
    assert recalculo.dias_afetados(pedido) == [DIA]
    pedido.periodo_fim = AGORA + timedelta(hours=15, minutes=15)
    assert recalculo.dias_afetados(pedido) == [DIA, date(2026, 11, 25)]
