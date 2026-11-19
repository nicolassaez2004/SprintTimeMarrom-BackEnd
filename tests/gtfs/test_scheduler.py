"""Ensaios locais da US02-T04: SQLite temporário, ZIPs sintéticos e relógio simulado."""

import io
import json
import logging
import zipfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock
from pathlib import Path

import pytest

from app import create_app, db
from app.coleta.bus_collector import LockColetor, NOME_LOCK_COLETOR
from app.coleta.qualidade import medir_cobertura
from app.gtfs import scheduler
from app.models import ArquivoArmazenado, VerificacaoGTFS, VersaoGTFS
from app.tarefas import __main__ as tarefas_cli

INICIO = datetime(2026, 11, 18, 12, tzinfo=timezone.utc)


class Relogio:
    def __init__(self):
        self.agora = INICIO
        self.esperas = []

    def __call__(self):
        return self.agora

    def avancar(self, segundos):
        self.agora += timedelta(seconds=segundos)

    def dormir(self, segundos):
        assert len(self.esperas) < 15000, "o agendador não chegou ao próximo ciclo"
        self.esperas.append(segundos)
        self.avancar(segundos)


def feed(versao="v1"):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as arquivo:
        for nome in ("agency.txt", "stops.txt", "routes.txt", "trips.txt",
                     "stop_times.txt", "calendar.txt"):
            entrada = zipfile.ZipInfo(nome, date_time=(2026, 11, 18, 12, 0, 0))
            entrada.compress_type = zipfile.ZIP_DEFLATED
            arquivo.writestr(entrada, "cabecalho\n" + versao + "\n")
    return buffer.getvalue()


class Fonte:
    def __init__(self, conteudo):
        self.conteudo = conteudo

    def baixar_para(self, url, destino):
        if isinstance(self.conteudo, Exception):
            raise self.conteudo
        destino.write(self.conteudo)


@pytest.fixture
def app(tmp_path):
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://",
                      "GTFS_STORAGE_DIR": None, "GTFS_TASK_LOG_FILE": None})
    app.instance_path = str(tmp_path / "instance")
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.engine.dispose()


@pytest.fixture
def relogio():
    return Relogio()


def eventos(caplog):
    return [json.loads(record.message) for record in caplog.records
            if record.name == "app.gtfs.scheduler"]


def atraso_commit(monkeypatch, relogio, segundos):
    commit = db.session.commit

    def confirmar():
        nova_versao = any(isinstance(item, VersaoGTFS) for item in db.session.new)
        commit()
        if nova_versao:
            relogio.avancar(segundos)

    monkeypatch.setattr(db.session, "commit", confirmar)


def test_nova_versao_mede_identificacao_ate_confirmacao_na_base(app, relogio, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="app.gtfs.scheduler")
    atraso_commit(monkeypatch, relogio, 4)
    resultado = scheduler.verificar_gtfs(relogio=relogio, cliente=Fonte(feed()))
    assert resultado["resultado"] == "NOVA"
    assert resultado["identificada_em"] == INICIO
    assert resultado["persistida_em"] == INICIO + timedelta(seconds=4)
    assert resultado["tempo_ate_base_segundos"] == 4
    assert resultado["prazo_24h_atendido"] is True
    execucao = db.session.get(VerificacaoGTFS, resultado["execucao_id"])
    assert (execucao.estado, execucao.resultado, execucao.versao_id) == ("OK", "NOVA", resultado["versao_id"])
    assert VersaoGTFS.query.count() == ArquivoArmazenado.query.count() == 1
    assert eventos(caplog)[-1]["versao_id"] == VersaoGTFS.query.one().id


def test_repetida_preserva_versao_data_e_zip_sem_falso_tempo_de_entrada(app, relogio):
    primeira = scheduler.verificar_gtfs(relogio=relogio, cliente=Fonte(feed()))
    relogio.avancar(86400)
    repetida = scheduler.verificar_gtfs(relogio=relogio, cliente=Fonte(feed()))
    assert repetida["resultado"] == "REPETIDA"
    assert repetida["versao_id"] == primeira["versao_id"]
    assert repetida["persistida_em"] is None
    assert repetida["tempo_ate_base_segundos"] is None
    assert repetida["prazo_24h_atendido"] is None
    assert VersaoGTFS.query.one().capturada_em == INICIO
    assert VersaoGTFS.query.count() == ArquivoArmazenado.query.count() == 1
    assert VerificacaoGTFS.query.count() == 2
    assert VerificacaoGTFS.query.order_by(VerificacaoGTFS.id.desc()).first().resultado == "REPETIDA"


def test_troca_valida_preserva_a_anterior_e_registra_a_nova(app, relogio):
    antiga = scheduler.verificar_gtfs(relogio=relogio, cliente=Fonte(feed("v1")))
    relogio.avancar(86400)
    nova = scheduler.verificar_gtfs(relogio=relogio, cliente=Fonte(feed("v2")))
    assert nova["resultado"] == "NOVA" and nova["versao_id"] != antiga["versao_id"]
    assert nova["prazo_24h_atendido"] is True
    assert VersaoGTFS.query.count() == 2
    for versao in VersaoGTFS.query.all():
        assert (Path(app.instance_path) / "gtfs" / f"{versao.hash}.zip").exists()


@pytest.mark.parametrize("conteudo,tipo", [(b"ZIP invalido", "GTFSValidationError"),
                                            (RuntimeError("senha-temporaria-na-url"), "RuntimeError")])
def test_falha_fica_registrada_sem_apagar_a_atual_ou_vazar_mensagem(app, relogio, caplog, conteudo, tipo):
    caplog.set_level(logging.INFO, logger="app.gtfs.scheduler")
    antiga = scheduler.verificar_gtfs(relogio=relogio, cliente=Fonte(feed()))
    relogio.avancar(86400)
    falha = scheduler.verificar_gtfs(relogio=relogio, cliente=Fonte(conteudo))
    assert falha["estado"] == "FALHA" and falha["tipo_erro"] == tipo
    assert falha["identificada_em"] is None
    assert falha["tempo_ate_base_segundos"] is None
    assert VersaoGTFS.query.one().id == antiga["versao_id"]
    assert db.session.get(VerificacaoGTFS, falha["execucao_id"]).erro == tipo
    assert "senha-temporaria-na-url" not in caplog.text
    assert eventos(caplog)[-1]["resultado"] == "FALHA"


@pytest.mark.parametrize("segundos,estado,atendeu", [(86400, "OK", True), (86401, "PARCIAL", False)])
def test_limite_de_24h_e_alerta_quando_excede(app, relogio, monkeypatch, caplog, segundos, estado, atendeu):
    atraso_commit(monkeypatch, relogio, segundos)
    resultado = scheduler.verificar_gtfs(relogio=relogio, cliente=Fonte(feed()))
    assert resultado["tempo_ate_base_segundos"] == segundos
    assert resultado["estado"] == estado
    assert resultado["prazo_24h_atendido"] is atendeu
    assert VersaoGTFS.query.count() == 1
    assert any(item["evento"] == "gtfs_prazo_excedido" for item in eventos(caplog)) is (not atendeu)


def test_verificacao_agendada_atrasada_emite_alerta(app, relogio, caplog):
    scheduler.verificar_gtfs(INICIO - timedelta(hours=2), relogio=relogio, cliente=Fonte(feed()))
    alerta = next(item for item in eventos(caplog) if item["evento"] == "gtfs_verificacao_atrasada")
    assert alerta["atraso_segundos"] == 7200


def test_processo_executa_diariamente_sem_derivar_com_a_duracao(app, relogio, monkeypatch):
    atraso_commit(monkeypatch, relogio, 4)

    def verificar(prevista, **kwargs):
        return scheduler.verificar_gtfs(prevista, cliente=Fonte(feed()), **kwargs)

    assert scheduler.executar_tarefas(relogio=relogio, dormir=relogio.dormir,
                                     verificar=verificar, max_ciclos=2) == 2
    execucoes = VerificacaoGTFS.query.order_by(VerificacaoGTFS.id).all()
    assert execucoes[1].iniciada_em - execucoes[0].iniciada_em == timedelta(days=1)
    assert execucoes[1].resultado == "REPETIDA"
    assert max(relogio.esperas) <= 60


def test_reinicio_respeita_ultima_verificacao_persistida(app, relogio):
    scheduler.verificar_gtfs(relogio=relogio, cliente=Fonte(feed()))
    relogio.avancar(3 * 3600)
    scheduler.executar_tarefas(relogio=relogio, dormir=relogio.dormir, max_ciclos=1,
        verificar=lambda prevista, **kwargs: scheduler.verificar_gtfs(prevista, cliente=Fonte(feed()), **kwargs))
    ultima = VerificacaoGTFS.query.order_by(VerificacaoGTFS.id.desc()).first()
    assert ultima.iniciada_em == INICIO + timedelta(days=1)
    assert sum(relogio.esperas) == 21 * 3600


def test_falha_tem_nova_tentativa_em_15_minutos(app, relogio):
    conteudos = iter([b"invalido", feed()])
    scheduler.executar_tarefas(relogio=relogio, dormir=relogio.dormir, max_ciclos=2,
        verificar=lambda prevista, **kwargs: scheduler.verificar_gtfs(prevista, cliente=Fonte(next(conteudos)), **kwargs))
    execucoes = VerificacaoGTFS.query.order_by(VerificacaoGTFS.id).all()
    assert [item.estado for item in execucoes] == ["FALHA", "OK"]
    assert execucoes[1].iniciada_em - execucoes[0].finalizada_em == timedelta(minutes=15)


def test_indisponibilidade_de_varios_dias_nao_dispara_rajada(app, relogio):
    scheduler.verificar_gtfs(relogio=relogio, cliente=Fonte(feed()))
    relogio.avancar(5 * 86400 + 3600)
    scheduler.executar_tarefas(relogio=relogio, dormir=relogio.dormir, max_ciclos=2,
        verificar=lambda prevista, **kwargs: scheduler.verificar_gtfs(prevista, cliente=Fonte(feed()), **kwargs))
    execucoes = VerificacaoGTFS.query.order_by(VerificacaoGTFS.id).all()
    assert execucoes[1].iniciada_em == INICIO + timedelta(days=5, hours=1)
    assert execucoes[2].iniciada_em == INICIO + timedelta(days=6)


def test_retomada_registra_execucao_interrompida_e_reverifica(app, relogio):
    db.session.add(VerificacaoGTFS(iniciada_em=INICIO,
                                prevista_em=INICIO, estado="EM_ANDAMENTO"))
    db.session.commit()
    relogio.avancar(3600)
    scheduler.executar_tarefas(relogio=relogio, dormir=relogio.dormir, max_ciclos=1,
        verificar=lambda prevista, **kwargs: scheduler.verificar_gtfs(prevista, cliente=Fonte(feed()), **kwargs))
    execucoes = VerificacaoGTFS.query.order_by(VerificacaoGTFS.id).all()
    assert execucoes[0].estado == "FALHA" and execucoes[0].erro == "ProcessoInterrompido"
    assert execucoes[1].estado == "OK" and execucoes[1].iniciada_em == relogio()


def test_perda_do_lock_impede_qualquer_verificacao(app, relogio):
    verificar = Mock()
    assert scheduler.executar_tarefas(relogio=relogio, manter_ativo=lambda: False,
                                     verificar=verificar, max_ciclos=1) == 0
    verificar.assert_not_called()
    assert VerificacaoGTFS.query.count() == 0


def test_lock_gtfs_e_separado_por_banco_e_do_gps():
    dev = SimpleNamespace(url=SimpleNamespace(database="sptrans_dev"))
    teste = SimpleNamespace(url=SimpleNamespace(database="sptrans_test"))
    assert scheduler.nome_lock_gtfs(dev) != scheduler.nome_lock_gtfs(teste)
    assert scheduler.nome_lock_gtfs(dev) != NOME_LOCK_COLETOR
    conexao = Mock()
    conexao.execute.return_value.scalar.return_value = 1
    engine = SimpleNamespace(dialect=SimpleNamespace(name="mysql"), connect=lambda: conexao)
    lock = LockColetor(engine, nome=scheduler.nome_lock_gtfs(dev))
    assert lock.adquirir()
    assert conexao.execute.call_args.args[1] == {"nome": scheduler.nome_lock_gtfs(dev)}
    lock.liberar()


def test_ciclo_gtfs_nao_infla_cobertura_de_gps(app, relogio):
    scheduler.verificar_gtfs(relogio=relogio, cliente=Fonte(feed()))
    cobertura = medir_cobertura(INICIO, INICIO + timedelta(minutes=1), 30)
    assert cobertura.ciclos_observados == 0
    assert cobertura.percentual == 0


def test_cli_uma_vez_guarda_log_json_e_libera_lock(app, relogio, monkeypatch):
    lock = Mock()
    lock.adquirir.return_value = True
    monkeypatch.setattr(tarefas_cli, "create_app", lambda: app)
    monkeypatch.setattr(tarefas_cli, "LockColetor", lambda *args, **kwargs: lock)
    monkeypatch.setattr(tarefas_cli, "agora_utc", relogio)
    monkeypatch.setattr(tarefas_cli, "verificar_gtfs", lambda prevista:
                        scheduler.verificar_gtfs(prevista, relogio=relogio, cliente=Fonte(feed())))
    assert tarefas_cli.main(["--uma-vez"]) == 0
    lock.liberar.assert_called_once()
    linhas = (Path(app.instance_path) / "logs" / "gtfs-tarefas.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(linhas[-1])["resultado"] == "NOVA"


def test_cli_recusa_segundo_processo_sem_capturar(app, monkeypatch):
    lock = Mock()
    lock.adquirir.return_value = False
    verificar = Mock()
    monkeypatch.setattr(tarefas_cli, "create_app", lambda: app)
    monkeypatch.setattr(tarefas_cli, "LockColetor", lambda *args, **kwargs: lock)
    monkeypatch.setattr(tarefas_cli, "verificar_gtfs", verificar)
    assert tarefas_cli.main(["--uma-vez"]) == 1
    verificar.assert_not_called()
    lock.liberar.assert_called_once()


@pytest.mark.parametrize("espera,atendeu", [(900, True), (90000, False)])
def test_primeira_identificacao_sobrevive_a_falha_sql_e_nao_reinicia_prazo(
    app, relogio, monkeypatch, espera, atendeu,
):
    commit = db.session.commit

    def falhar_so_insercao_da_versao():
        if any(isinstance(item, VersaoGTFS) for item in db.session.new):
            raise RuntimeError("falha SQL simulada")
        return commit()

    with monkeypatch.context() as patch:
        patch.setattr(db.session, "commit", falhar_so_insercao_da_versao)
        falha = scheduler.verificar_gtfs(relogio=relogio, cliente=Fonte(feed()))
    assert falha["estado"] == "FALHA"
    assert falha["identificada_em"] == INICIO
    assert VerificacaoGTFS.query.one().identificada_em == INICIO
    assert VersaoGTFS.query.count() == 0
    db.session.remove()  # Simula a sessão nova usada depois de reiniciar.
    relogio.avancar(espera)
    retomada = scheduler.verificar_gtfs(relogio=relogio, cliente=Fonte(feed()))
    assert retomada["resultado"] == "NOVA"
    assert retomada["identificada_em"] == INICIO
    assert retomada["tempo_ate_base_segundos"] == espera
    assert retomada["prazo_24h_atendido"] is atendeu
    assert VerificacaoGTFS.query.order_by(VerificacaoGTFS.id.desc()).first().tempo_ate_base_segundos == espera


def test_api_nao_inicia_tarefas_ao_ser_importada(app, monkeypatch):
    executar = Mock(side_effect=AssertionError("não iniciar tarefas no Flask"))
    monkeypatch.setattr(scheduler, "executar_tarefas", executar)
    outra = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://"})
    assert outra.test_client().get("/health").status_code == 200
    executar.assert_not_called()


def test_migracao_0005_corresponde_aos_modelos_e_compila_para_mysql(app):
    import importlib.util
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine

    arquivo = Path(__file__).resolve().parents[2] / "migrations/versions/0005_verificacoes_gtfs.py"
    spec = importlib.util.spec_from_file_location("migracao_gtfs", arquivo)
    migracao = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migracao)
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as conexao:
            antigas = [table for table in db.metadata.sorted_tables if table.name != "verificacoes_gtfs"]
            db.metadata.create_all(conexao, tables=antigas)
            context = MigrationContext.configure(conexao)
            with Operations.context(context):
                migracao.upgrade()
            assert compare_metadata(context, db.metadata) == []
        saida = io.StringIO()
        offline = MigrationContext.configure(dialect_name="mysql", opts={"as_sql": True, "output_buffer": saida})
        with Operations.context(offline):
            migracao.upgrade()
        sql = saida.getvalue()
        assert "DATETIME(6)" in sql and "REFERENCES versoes_gtfs (id)" in sql
        assert "CREATE TABLE verificacoes_gtfs" in sql
    finally:
        engine.dispose()


def test_cadeia_de_migracoes_tem_uma_cabeca_e_preserva_a_us12():
    from alembic.script import ScriptDirectory

    scripts = ScriptDirectory(str(Path(__file__).resolve().parents[2] / "migrations"))
    assert scripts.get_heads() == ["0008"]
    assert scripts.get_revision("0008").down_revision == "0007"
    assert scripts.get_revision("0007").down_revision == "0006"
    assert scripts.get_revision("0006").down_revision == "0005"
    assert scripts.get_revision("0005").down_revision == "0004"
    assert scripts.get_revision("0004").module.__file__.endswith("0004_observacoes_tratadas.py")


def test_migracao_0008_cria_manifesto_e_resultados_por_execucao():
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from alembic.script import ScriptDirectory

    scripts = ScriptDirectory(str(Path(__file__).resolve().parents[2] / "migrations"))
    migracao = scripts.get_revision("0008").module
    saida = io.StringIO()
    offline = MigrationContext.configure(
        dialect_name="mysql", opts={"as_sql": True, "output_buffer": saida}
    )
    with Operations.context(offline):
        migracao.upgrade()
    sql = saida.getvalue()
    assert "CREATE TABLE execucoes_processamento" in sql
    assert "bruto_id_maximo BIGINT NOT NULL" in sql
    assert "ADD COLUMN execucao_processamento_id VARCHAR(36)" in sql
    assert "uq_observacoes_execucao_bruto" in sql
    assert "fk_observacoes_execucao" in sql
