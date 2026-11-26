"""US09-T02: catálogo, download, dicionário e publicação agendada dos brutos."""

import io
import json
import logging
from datetime import date, datetime, timedelta, timezone
from unittest.mock import Mock

import pyarrow.parquet as pq
import pytest

from app import create_app, db
from app.exportacoes import parquet
from app.api import downloads as routes
from app.exportacoes.parquet import (
    FUSO, arquivo_completo, datas_disponiveis, dia_a_publicar, gerar_parquet_diario,
    nome_arquivo, publicar_brutos,
)
from app.gtfs import scheduler
from app.models import ExecucaoColeta, RegistroBrutoGPS
from app.tarefas import __main__ as tarefas

UTC = timezone.utc
DIA = date(2026, 11, 18)


def sp(*args):
    return datetime(*args, tzinfo=FUSO).astimezone(UTC)


def relogio_fixo(instante):
    return lambda: instante


@pytest.fixture
def pasta(tmp_path):
    return tmp_path / "exportacoes"


@pytest.fixture
def app(pasta):
    aplicacao = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://",
                            "MYSQL_SSL_CA": None, "EXPORTACOES_DIR": str(pasta)})
    with aplicacao.app_context():
        db.create_all()
        execucao = ExecucaoColeta(iniciada_em=sp(2026, 11, 16, 12))
        db.session.add(execucao)
        db.session.flush()
        for dia, prefixo in ((date(2026, 11, 16), "A"), (date(2026, 11, 17), "B"), (DIA, "C")):
            for minuto in (0, 30):
                db.session.add(RegistroBrutoGPS(
                    execucao_id=execucao.id, prefixo_origem=f"{prefixo}{minuto}",
                    coletado_em=sp(dia.year, dia.month, dia.day, 12, minuto),
                    latitude=-23.5, longitude=-46.6, payload={"p": prefixo}))
        # Último instante do dia em São Paulo e primeiro do dia seguinte.
        db.session.add(RegistroBrutoGPS(execucao_id=execucao.id, prefixo_origem="fim",
                                        coletado_em=sp(2026, 11, 18, 23, 59, 59, 999999)))
        db.session.add(RegistroBrutoGPS(execucao_id=execucao.id, prefixo_origem="seguinte",
                                        coletado_em=sp(2026, 11, 19, 0, 0)))
        db.session.commit()
        yield aplicacao
        db.session.remove()
        db.engine.dispose()


@pytest.fixture
def cliente(app, monkeypatch):
    monkeypatch.setattr(routes, "agora_utc", lambda: sp(2026, 11, 19, 12))
    return app.test_client()


def exportar(pasta, dia, gerado_em=None):
    gerado_em = gerado_em or sp(dia.year, dia.month, dia.day, 0, 30) + timedelta(days=1)
    return gerar_parquet_diario(dia, pasta / nome_arquivo(dia), relogio=relogio_fixo(gerado_em))


# --- Catálogo -----------------------------------------------------------------

def test_lista_somente_datas_completas_da_mais_recente(app, cliente, pasta):
    exportar(pasta, DIA)
    exportar(pasta, date(2026, 11, 16))
    corpo = cliente.get("/downloads/brutos/datas").get_json()
    assert [item["data"] for item in corpo["datas"]] == ["2026-11-18", "2026-11-16"]
    assert corpo["total"] == 2 and corpo["licenca"] == "CC BY 4.0"
    assert corpo["datas"][0] == {
        "data": "2026-11-18", "registros": 3, "tamanho_bytes": (pasta / nome_arquivo(DIA)).stat().st_size,
        "gerado_em": sp(2026, 11, 19, 0, 30).isoformat(), "url": "/downloads/brutos?data=2026-11-18"}
    assert str(pasta) not in json.dumps(corpo)  # sem caminhos internos
    assert cliente.get("/api/downloads/brutos/datas").get_json() == corpo


def test_catalogo_vazio_sem_diretorio(cliente):
    assert cliente.get("/downloads/brutos/datas").get_json()["datas"] == []


def test_exclui_arquivos_incompletos_temporarios_e_invalidos(app, cliente, pasta):
    exportar(pasta, DIA)
    # Exportação manual antes do fechamento do dia: parcial.
    exportar(pasta, date(2026, 11, 17), gerado_em=sp(2026, 11, 17, 20))
    # Exatamente no limite de fechamento + margem: já é completo.
    exportar(pasta, date(2026, 11, 16), gerado_em=sp(2026, 11, 17, 0, 10))
    # Em geração: temporário da T01, oculto e com sufixo .tmp.
    (pasta / f".{nome_arquivo(date(2026, 11, 15))}.abc.tmp").write_bytes(b"PAR1parcial")
    # Truncado/corrompido com nome final.
    (pasta / nome_arquivo(date(2026, 11, 14))).write_bytes(b"PAR1 truncado")
    # Arquivo de outro dia renomeado: metadado date não confere.
    (pasta / nome_arquivo(date(2026, 11, 13))).write_bytes((pasta / nome_arquivo(DIA)).read_bytes())
    (pasta / "registros_gps_2026-02-30.parquet").write_bytes(b"")
    (pasta / "outro.parquet").write_bytes(b"")
    assert [item["data"] for item in datas_disponiveis(pasta)] == ["2026-11-18", "2026-11-16"]
    for dia in ("2026-11-17", "2026-11-15", "2026-11-14", "2026-11-13"):
        resposta = cliente.get(f"/downloads/brutos?data={dia}")
        assert resposta.status_code == 404, dia
        assert resposta.get_json()["erro"]["codigo"] == "ARQUIVO_INEXISTENTE"


def test_dez_minutos_antes_da_margem_ainda_e_parcial(app, pasta):
    exportar(pasta, DIA, gerado_em=sp(2026, 11, 19, 0, 9, 59))
    assert arquivo_completo(pasta, DIA) is None


# --- Download -----------------------------------------------------------------

def test_download_entrega_o_parquet_correto(app, cliente, pasta):
    exportar(pasta, DIA)
    exportar(pasta, date(2026, 11, 17))
    for prefixo in ("", "/api"):
        resposta = cliente.get(f"{prefixo}/downloads/brutos?data=2026-11-18")
        assert resposta.status_code == 200
        assert resposta.mimetype == "application/vnd.apache.parquet"
        disposicao = resposta.headers["Content-Disposition"]
        assert disposicao.startswith("attachment") and "registros_gps_2026-11-18.parquet" in disposicao
        assert resposta.headers["X-Content-Type-Options"] == "nosniff"
        assert resposta.data == (pasta / nome_arquivo(DIA)).read_bytes()
        tabela = pq.read_table(io.BytesIO(resposta.data))
        assert sorted(tabela.column("prefixo_origem").to_pylist()) == ["C0", "C30", "fim"]
        assert tabela.schema.metadata[b"date"] == b"2026-11-18"
        resposta.close()


@pytest.mark.parametrize("consulta", [
    "", "data=", "data=2026-13-01", "data=2026-02-30", "data=18/11/2026", "data=2026-11-18T00:00",
    "data=2026-11-18&data=2026-11-17", "data=2026-11-18&formato=csv",
])
def test_datas_e_parametros_invalidos(cliente, consulta):
    resposta = cliente.get(f"/downloads/brutos?{consulta}")
    assert resposta.status_code == 400
    erro = resposta.get_json()["erro"]
    assert erro["codigo"] == "PARAMETRO_INVALIDO" and erro["mensagem"]


def test_dia_ainda_nao_consolidado_informa_previsao(app, pasta, monkeypatch):
    cliente = app.test_client()
    monkeypatch.setattr(routes, "agora_utc", lambda: sp(2026, 11, 19, 5, 59, 59))
    for dia in ("2026-11-18", "2026-11-19", "2026-12-25"):
        resposta = cliente.get(f"/downloads/brutos?data={dia}")
        assert resposta.status_code == 404
        assert resposta.get_json()["erro"]["codigo"] == "ARQUIVO_AINDA_INDISPONIVEL"
    previsto = cliente.get("/downloads/brutos?data=2026-11-18").get_json()["erro"]["previsto_ate"]
    assert datetime.fromisoformat(previsto) == sp(2026, 11, 19, 6)
    # A partir das 06:00 de D+1, ausência é arquivo inexistente.
    monkeypatch.setattr(routes, "agora_utc", lambda: sp(2026, 11, 19, 6))
    assert cliente.get("/downloads/brutos?data=2026-11-18").get_json()["erro"]["codigo"] == "ARQUIVO_INEXISTENTE"


def test_sem_escrita_publica(cliente):
    assert cliente.post("/downloads/brutos?data=2026-11-18").status_code == 405
    assert cliente.delete("/downloads/brutos?data=2026-11-18").status_code == 405


# --- Dicionário ---------------------------------------------------------------

def test_dicionario_corresponde_ao_esquema_real_do_arquivo(app, cliente, pasta):
    exportar(pasta, DIA)
    corpo = cliente.get("/downloads/brutos/dicionario").get_json()
    assert cliente.get("/api/downloads/brutos/dicionario").status_code == 200
    esquema = pq.read_schema(pasta / nome_arquivo(DIA))
    assert [(c["nome"], c["tipo"], c["anulavel"]) for c in corpo["campos"]] == [
        (campo.name, str(campo.type), campo.nullable) for campo in esquema]
    assert all(campo["descricao"] for campo in corpo["campos"])
    unidades = {campo["nome"]: campo["unidade"] for campo in corpo["campos"]}
    assert unidades["latitude"] == unidades["longitude"] == "graus decimais"
    assert "UTC" in unidades["coletado_em"] and "UTC" in unidades["horario_gps"]
    assert set(corpo["metadados_arquivo"]) == {chave.decode() for chave in esquema.metadata
                                               if not chave.startswith(b"ARROW")}
    assert corpo["licenca"] == "CC BY 4.0" and corpo["fonte_url"].startswith("https://")


# --- Virada de dia e horário de publicação -------------------------------------

@pytest.mark.parametrize("agora, esperado", [
    (sp(2026, 11, 19, 0, 0), date(2026, 11, 17)),
    (sp(2026, 11, 19, 0, 9, 59), date(2026, 11, 17)),
    (sp(2026, 11, 19, 0, 10), date(2026, 11, 18)),
    (sp(2026, 11, 19, 5, 59, 59), date(2026, 11, 18)),
    (sp(2026, 11, 19, 6, 0), date(2026, 11, 18)),
    (sp(2026, 11, 19, 23, 59), date(2026, 11, 18)),
    # 02:30 UTC ainda é dia 18 em São Paulo (UTC-3).
    (datetime(2026, 11, 19, 2, 30, tzinfo=UTC), date(2026, 11, 17)),
])
def test_dia_a_publicar_na_virada(agora, esperado):
    assert dia_a_publicar(agora) == esperado


@pytest.mark.parametrize("publicado_em, no_prazo", [
    (sp(2026, 11, 19, 0, 10), True),
    (sp(2026, 11, 19, 6, 0), True),
    (sp(2026, 11, 19, 6, 0, 1), False),
])
def test_publicacao_e_limite_das_06h(app, pasta, caplog, publicado_em, no_prazo):
    caplog.set_level(logging.INFO, logger="app.exportacoes.parquet")
    resultado = publicar_brutos(sp(2026, 11, 19, 0, 10), relogio=relogio_fixo(publicado_em))
    assert resultado["data"] == "2026-11-18" and resultado["registros"] == 3
    assert resultado["prazo_atendido"] is no_prazo
    evento = json.loads(caplog.records[-1].getMessage())["evento"]
    assert evento == ("brutos_parquet_publicado" if no_prazo else "brutos_parquet_prazo_excedido")
    assert [item["data"] for item in datas_disponiveis(pasta)] == ["2026-11-18"]


def test_publicacao_idempotente_e_substitui_exportacao_parcial(app, pasta, monkeypatch):
    exportar(pasta, DIA, gerado_em=sp(2026, 11, 18, 20))  # manual, dia ainda aberto
    assert datas_disponiveis(pasta) == []
    # 00:09: o dia 18 ainda está na margem; a tarefa cuida do dia 17.
    assert publicar_brutos(sp(2026, 11, 19, 0, 9),
                           relogio=relogio_fixo(sp(2026, 11, 19, 0, 9)))["data"] == "2026-11-17"
    assert arquivo_completo(pasta, DIA) is None
    # 00:15: o parcial do dia 18 é substituído pela exportação completa.
    assert publicar_brutos(sp(2026, 11, 19, 0, 15),
                           relogio=relogio_fixo(sp(2026, 11, 19, 0, 15)))["data"] == "2026-11-18"
    assert [item["data"] for item in datas_disponiveis(pasta)] == ["2026-11-18", "2026-11-17"]
    gerar = Mock()
    monkeypatch.setattr(parquet, "gerar_parquet_diario", gerar)
    assert publicar_brutos(sp(2026, 11, 19, 3)) is None
    gerar.assert_not_called()


# --- Integração ao processo de tarefas -----------------------------------------

class Relogio:
    def __init__(self, inicio):
        self.agora = inicio

    def __call__(self):
        return self.agora

    def avancar(self, **delta):
        self.agora += timedelta(**delta)


def test_tarefa_periodica_roda_no_loop_e_falha_nao_afeta_gtfs(app):
    relogio = Relogio(sp(2026, 11, 19, 0, 10))
    chamadas = []

    def periodica(agora):
        chamadas.append(agora)
        if len(chamadas) == 1:
            raise RuntimeError("banco indisponível")

    verificar = Mock(side_effect=lambda prevista, **_: relogio.avancar(minutes=5))
    assert scheduler.executar_tarefas(relogio=relogio, dormir=Mock(), verificar=verificar,
                                     max_ciclos=5, periodicas=(periodica,)) == 5
    assert verificar.call_count == 5
    inicio = sp(2026, 11, 19, 0, 10)
    # Falha em 00:10; nova tentativa só após 15 min (00:25), depois a cada volta.
    assert chamadas == [inicio, inicio + timedelta(minutes=15), inicio + timedelta(minutes=20)]


def test_loop_publica_o_parquet_do_dia_anterior(app, pasta):
    relogio = Relogio(sp(2026, 11, 19, 0, 15))
    scheduler.executar_tarefas(relogio=relogio, dormir=Mock(), verificar=Mock(), max_ciclos=2,
                               periodicas=(publicar_brutos,))
    assert [item["data"] for item in datas_disponiveis(pasta)] == ["2026-11-18"]


def test_processo_de_tarefas_registra_a_publicacao(app, monkeypatch, tmp_path):
    capturado = {}
    lock = Mock()
    lock.adquirir.return_value = True
    app.config["GTFS_TASK_LOG_FILE"] = str(tmp_path / "tarefas.jsonl")
    monkeypatch.setattr(tarefas, "create_app", lambda: app)
    monkeypatch.setattr(tarefas, "LockColetor", lambda *a, **k: lock)
    monkeypatch.setattr(tarefas, "executar_tarefas", lambda **kwargs: capturado.update(kwargs))
    tarefas.main([])
    from app.contexto.clima.coleta import coletar_clima  # US06-T02
    from app.indicadores.agenda import atualizar_indicadores  # US23-T03
    from app.contexto.infraestrutura.tarefa import atualizar_infraestrutura  # US07
    assert capturado["periodicas"] == (publicar_brutos, atualizar_indicadores,
                                       atualizar_infraestrutura, coletar_clima)


# --- Review: dados gravados com atraso (US29) e data limite --------------------

def ciclo(iniciada_em, finalizada_em=None, estado="OK"):
    execucao = ExecucaoColeta(iniciada_em=iniciada_em, finalizada_em=finalizada_em, estado=estado)
    db.session.add(execucao)
    db.session.commit()
    return execucao


def test_espera_ciclo_em_andamento_do_dia_ate_o_prazo(app, pasta):
    ciclo(sp(2026, 11, 18, 23, 59, 30), estado="EM_ANDAMENTO")
    assert publicar_brutos(sp(2026, 11, 19, 0, 15), relogio=relogio_fixo(sp(2026, 11, 19, 0, 15))) is None
    assert arquivo_completo(pasta, DIA) is None
    # Ciclo de depois da meia-noite em andamento não bloqueia o dia anterior.
    ciclo(sp(2026, 11, 19, 0, 0, 30), estado="EM_ANDAMENTO")
    # No prazo, publica mesmo com o ciclo travado, para não perder as 06:00.
    resultado = publicar_brutos(sp(2026, 11, 19, 6), relogio=relogio_fixo(sp(2026, 11, 19, 6)))
    assert resultado["data"] == "2026-11-18" and resultado["prazo_atendido"] is True
    # Ciclo travado sem finalizada_em não provoca regeração a cada minuto.
    gerar = Mock()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(parquet, "gerar_parquet_diario", gerar)
        assert publicar_brutos(sp(2026, 11, 19, 6, 1)) is None
    gerar.assert_not_called()


def test_regera_quando_ciclo_do_dia_e_gravado_depois_da_exportacao(app, pasta, caplog):
    caplog.set_level(logging.INFO, logger="app.exportacoes.parquet")
    publicar_brutos(sp(2026, 11, 19, 0, 15), relogio=relogio_fixo(sp(2026, 11, 19, 0, 15)))
    assert arquivo_completo(pasta, DIA)["registros"] == 3
    # MySQL fora às 23:59: o checkpoint grava o ciclo do dia 18 só à 01:00.
    atrasado = ciclo(sp(2026, 11, 18, 23, 59, 40), finalizada_em=sp(2026, 11, 19, 1))
    db.session.add(RegistroBrutoGPS(execucao_id=atrasado.id, prefixo_origem="atrasado",
                                    coletado_em=sp(2026, 11, 18, 23, 59, 40)))
    db.session.commit()
    resultado = publicar_brutos(sp(2026, 11, 19, 1, 1), relogio=relogio_fixo(sp(2026, 11, 19, 1, 1)))
    assert resultado["data"] == "2026-11-18" and resultado["registros"] == 4
    assert json.loads(caplog.records[-1].getMessage())["motivo"] == "DADO_ATRASADO"
    tabela = pq.read_table(pasta / nome_arquivo(DIA))
    assert "atrasado" in tabela.column("prefixo_origem").to_pylist()
    # Já regerado: não repete.
    assert publicar_brutos(sp(2026, 11, 19, 1, 2), relogio=relogio_fixo(sp(2026, 11, 19, 1, 2))) is None


def test_revisao_de_atrasos_limitada_aos_ultimos_dias(app, pasta):
    antigo = date(2026, 11, 10)
    exportar(pasta, antigo)
    ciclo(sp(2026, 11, 10, 12), finalizada_em=sp(2026, 11, 19))
    gerar = Mock()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(parquet, "gerar_parquet_diario", gerar)
        mp.setattr(parquet, "_ciclo_em_aberto", lambda dia: True)  # isola a revisão
        publicar_brutos(sp(2026, 11, 19, 3))
    gerar.assert_not_called()  # 10/11 está fora dos REVISAO_DIAS: usar a CLI


def test_data_maxima_nao_gera_erro_interno(cliente):
    resposta = cliente.get("/downloads/brutos?data=9999-12-31")
    assert resposta.status_code == 404
    erro = resposta.get_json()["erro"]
    assert erro["codigo"] == "ARQUIVO_AINDA_INDISPONIVEL" and erro["previsto_ate"] is None
    assert cliente.get("/downloads/brutos?data=0001-01-01").status_code == 404
