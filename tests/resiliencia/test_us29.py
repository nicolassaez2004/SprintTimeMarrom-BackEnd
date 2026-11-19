from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock
import json
import pytest
from sqlalchemy import event
from sqlalchemy.exc import OperationalError
from app import create_app, db
from app.models import ExecucaoColeta, LacunaColeta, RegistroBrutoGPS, NotificacaoColeta
from app.coleta.resiliencia import CicloResiliente
from app.coleta.retentativas import temporaria
from app.integrations.sptrans import (
    SPTransConexaoError,
    SPTransHTTPError,
    SPTransConfiguracaoError,
)
from app.repositories.coleta import ColetaRepository, PossePerdida
from app.monitoramento.journal import JournalColeta
from app.monitoramento.metricas import medir_gravacao


class Clock:
    def __init__(self):
        self.inicio = datetime(2026, 11, 19, 15, tzinfo=timezone.utc)
        self.t = 0
        self.esperas = []

    def agora(self):
        return self.inicio + timedelta(seconds=self.t)

    def monotonic(self):
        return self.t

    def dormir(self, s):
        self.esperas.append(s)
        self.t += s


@pytest.fixture
def contexto(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": "sqlite://",
            "COLETA_STATE_DIR": str(tmp_path),
        }
    )
    with app.app_context():
        event.listen(
            db.engine, "connect", lambda c, _: c.execute("PRAGMA foreign_keys=ON")
        )
        db.create_all()
        yield app
        db.session.remove()
        db.engine.dispose()


def resposta(clock, *, sem_horario=False):
    v = dict(p="5810", py=-23.55, px=-46.63)
    if not sem_horario:
        v["ta"] = clock.agora().isoformat()
    return dict(hr="12:00", l=[dict(cl=123, sl=1, vs=[v])])


def montar(tmp_path, clock, cliente, repository=None):
    journal = JournalColeta(tmp_path / "checkpoint", agora=clock.agora)
    repo = repository or ColetaRepository()
    repo.assumir()
    factory = Mock(return_value=cliente)
    service = CicloResiliente(
        journal, repo, factory, monotonic=clock.monotonic, dormir=clock.dormir
    )
    return service, journal, repo, factory


@pytest.mark.parametrize(
    "status,esperado",
    [
        (400, False),
        (401, False),
        (403, False),
        (408, True),
        (429, True),
        (500, True),
        (503, True),
    ],
)
def test_classificacao_http_sem_mensagem_sensivel(status, esperado):
    assert temporaria(SPTransHTTPError("/Posicao", status)) is esperado
    assert not temporaria(SPTransConfiguracaoError("TLS"))


@pytest.mark.parametrize(
    "codigo,esperado",
    [
        (1045, False),
        (1054, False),
        (2003, True),
        (2013, True),
        (1205, True),
        (1213, True),
    ],
)
def test_classificacao_mysql_separa_configuracao_de_indisponibilidade(codigo, esperado):
    assert (
        temporaria(OperationalError("sql", {}, Exception(codigo, "detalhe privado")))
        is esperado
    )


def test_cinco_retornos_e_sexta_tentativa_bem_sucedida(contexto, tmp_path):
    clock = Clock()
    client = SimpleNamespace(
        buscar_todas_posicoes=Mock(
            side_effect=[SPTransConexaoError("segredo") for _ in range(5)]
            + [resposta(clock)]
        )
    )
    service, journal, _, factory = montar(tmp_path, clock, client)
    assert service.coletar_ciclo(clock.agora()) == 1
    assert clock.esperas == [1, 2, 4, 8, 16] and factory.call_count == 6
    e = ExecucaoColeta.query.one()
    assert e.tentativas == 6 and e.estado == "OK" and e.erro is None
    assert e.duracao_gravacao_ms == 31000 and e.confirmada_em is not None
    assert RegistroBrutoGPS.query.count() == 1
    assert LacunaColeta.query.one().fim is not None
    assert journal.dados["ciclo"] is None


def test_para_na_sexta_falha_e_retoma_no_ciclo_seguinte(contexto, tmp_path):
    clock = Clock()
    client = SimpleNamespace(
        buscar_todas_posicoes=Mock(side_effect=SPTransConexaoError("token=nao-vazar"))
    )
    service, journal, repo, factory = montar(tmp_path, clock, client)
    assert service.coletar_ciclo() == 0
    assert factory.call_count == 6 and clock.esperas == [1, 2, 4, 8, 16]
    assert RegistroBrutoGPS.query.count() == 0
    assert ExecucaoColeta.query.one().estado == "FALHA"
    assert "nao-vazar" not in journal.caminho.read_text()
    client.buscar_todas_posicoes = Mock(return_value=resposta(clock))
    assert service.coletar_ciclo() == 1
    assert ExecucaoColeta.query.count() == 2 and LacunaColeta.query.count() == 1
    assert LacunaColeta.query.one().fim is not None


def test_falha_permanente_nao_gasta_cinco_tentativas(contexto, tmp_path):
    clock = Clock()
    client = SimpleNamespace(
        buscar_todas_posicoes=Mock(side_effect=SPTransHTTPError("/Posicao", 403))
    )
    service, _, _, factory = montar(tmp_path, clock, client)
    assert (
        service.coletar_ciclo() == 0 and factory.call_count == 1 and not clock.esperas
    )
    assert ExecucaoColeta.query.one().erro == "SPTRANS_HTTP_403"


def test_commit_com_resposta_perdida_e_reinicio_nao_duplicam_nem_gps_sem_chave(
    contexto, tmp_path, monkeypatch
):
    clock = Clock()
    client = SimpleNamespace(
        buscar_todas_posicoes=Mock(return_value=resposta(clock, sem_horario=True))
    )
    service, journal, repo, factory = montar(tmp_path, clock, client)
    original = repo.concluir

    def commit_sem_resposta(*args):
        original(*args)
        raise OperationalError("sql", {}, Exception("conexao perdida"))

    monkeypatch.setattr(repo, "concluir", commit_sem_resposta)
    with pytest.raises(OperationalError):
        service.coletar_ciclo()
    assert RegistroBrutoGPS.query.count() == 1
    id = RegistroBrutoGPS.query.one().id
    clock.dormir(70)
    retomada, j2, _, f2 = montar(
        tmp_path,
        clock,
        SimpleNamespace(
            buscar_todas_posicoes=Mock(
                side_effect=AssertionError("nao consultar de novo")
            )
        ),
    )
    assert retomada.coletar_ciclo() == 1 and f2.call_count == 0
    assert RegistroBrutoGPS.query.one().id == id and ExecucaoColeta.query.count() == 1
    assert ExecucaoColeta.query.one().duracao_gravacao_ms == 70000
    assert j2.dados["ciclo"] is None


def test_limite_de_tentativas_sobrevive_ao_reinicio(contexto, tmp_path):
    clock = Clock()
    client = SimpleNamespace(
        buscar_todas_posicoes=Mock(side_effect=SPTransConexaoError("timeout"))
    )
    service, journal, _, _ = montar(tmp_path, clock, client)
    c = journal.iniciar_ciclo()
    c["tentativas"] = 3
    journal.salvar()
    retomada, _, _, factory = montar(tmp_path, clock, client)
    assert retomada.coletar_ciclo() == 0 and factory.call_count == 3
    assert ExecucaoColeta.query.one().tentativas == 6


def test_reinicio_respeita_espera_que_ainda_nao_terminou(contexto, tmp_path):
    clock = Clock()
    client = SimpleNamespace(buscar_todas_posicoes=lambda: resposta(clock))
    _, journal, _, _ = montar(tmp_path, clock, client)
    journal.iniciar_ciclo()
    journal.atualizar_ciclo(
        tentativas=2, retentar_em=(clock.agora() + timedelta(seconds=4)).isoformat()
    )
    clock.dormir(1)
    clock.esperas = []
    service, _, _, _ = montar(tmp_path, clock, client)
    service.coletar_ciclo()
    assert clock.esperas == [3] and ExecucaoColeta.query.one().tentativas == 3


def test_alerta_no_limite_600s_reinicio_recuperacao_e_intermitencia(contexto, tmp_path):
    clock = Clock()
    client = SimpleNamespace(buscar_todas_posicoes=Mock(return_value=resposta(clock)))
    service, journal, repo, _ = montar(tmp_path, clock, client)
    journal.abrir_lacuna("TIMEOUT")
    clock.dormir(599)
    journal.verificar_alerta()
    assert not list((tmp_path / "checkpoint/alertas").glob("*.json"))
    clock.dormir(1)
    journal.verificar_alerta()
    assert len(list((tmp_path / "checkpoint/alertas").glob("*.json"))) == 1
    journal = JournalColeta(tmp_path / "checkpoint", agora=clock.agora)
    journal.verificar_alerta()
    journal.verificar_alerta()
    assert len(journal.dados["alertas"]) == 1
    service = CicloResiliente(
        journal, repo, lambda: client, monotonic=clock.monotonic, dormir=clock.dormir
    )
    service.coletar_ciclo()
    assert len(journal.dados["alertas"]) == 2
    assert {a.tipo for a in NotificacaoColeta.query.all()} == {"FALHA", "RECUPERACAO"}
    assert LacunaColeta.query.one().fim == clock.agora()
    journal.abrir_lacuna("FALHA_CURTA")
    clock.dormir(30)
    service.coletar_ciclo()
    assert len(journal.dados["alertas"]) == 2 and LacunaColeta.query.count() == 2


@pytest.mark.parametrize("segundos,rapido", [(60, True), (60.001, False)])
def test_confirmacao_60s_inclusiva(contexto, tmp_path, segundos, rapido):
    clock = Clock()

    def buscar():
        clock.dormir(segundos)
        return resposta(clock)

    service, _, _, _ = montar(
        tmp_path, clock, SimpleNamespace(buscar_todas_posicoes=buscar)
    )
    service.coletar_ciclo(clock.inicio)
    r = medir_gravacao(clock.inicio, clock.inicio + timedelta(seconds=30))
    assert r["confirmados_ate_60s"] == int(rapido)
    assert not r["observacao_mensal_comprovada"]


def test_ciclos_ausentes_e_confirmacao_desconhecida_nao_inflam_99(contexto):
    clock = Clock()
    db.session.add(
        ExecucaoColeta(iniciada_em=clock.agora(), estado="OK", fonte="OLHO_VIVO")
    )
    db.session.commit()
    r = medir_gravacao(clock.inicio, clock.inicio + timedelta(minutes=10))
    assert r["denominador"] == 20 and r["confirmacao_desconhecida"] == 1
    assert r["sem_ciclo_registrado"] == 19 and not r["meta_atingida"]


def test_posse_trocada_impede_coletor_antigo_de_publicar(contexto, tmp_path):
    clock = Clock()
    client = SimpleNamespace(buscar_todas_posicoes=Mock(return_value=resposta(clock)))
    service, journal, repo, factory = montar(tmp_path, clock, client)
    novo = ColetaRepository()
    novo.assumir()
    with pytest.raises(PossePerdida):
        service.coletar_ciclo()
    assert factory.call_count == 0 and RegistroBrutoGPS.query.count() == 0


def test_entrada_publica_do_coletor_usa_worker_e_retorna_quantidade(
    contexto, tmp_path, monkeypatch
):
    from app.coleta import bus_collector

    clock = Clock()
    payload = resposta(clock)
    payload["l"][0]["vs"].append({**payload["l"][0]["vs"][0], "p": "outro"})
    monkeypatch.setattr(
        bus_collector,
        "SPTransClient",
        lambda: SimpleNamespace(buscar_todas_posicoes=lambda: payload),
    )
    lock = SimpleNamespace(
        adquirir=lambda: True, ativo=lambda: True, liberar=lambda: None
    )
    monkeypatch.setattr(bus_collector, "LockColetor", lambda _: lock)
    assert bus_collector.BusCollector().coletar_ciclo(clock.agora()) == 2
    assert RegistroBrutoGPS.query.count() == 2
    assert ExecucaoColeta.query.one().prevista_em == clock.agora()


def test_banco_indisponivel_checkpoint_retoma_sem_fabricar_posicoes(
    contexto, tmp_path, monkeypatch
):
    clock = Clock()
    client = SimpleNamespace(buscar_todas_posicoes=lambda: resposta(clock))
    service, journal, repo, factory = montar(tmp_path, clock, client)
    monkeypatch.setattr(
        repo,
        "inicio",
        Mock(side_effect=OperationalError("sql", {}, Exception("offline"))),
    )
    with pytest.raises(OperationalError):
        service.coletar_ciclo()
    assert factory.call_count == 0 and RegistroBrutoGPS.query.count() == 0
    clock.dormir(600)
    retomada, j2, repo2, f2 = montar(tmp_path, clock, client)
    repo2.restaurar(j2, 30, clock.agora())
    assert retomada.coletar_ciclo() == 1 and f2.call_count == 1
    assert RegistroBrutoGPS.query.one().coletado_em == clock.agora()
    assert ExecucaoColeta.query.one().duracao_gravacao_ms == 600000
    assert NotificacaoColeta.query.count() == 2


def test_reinicio_recupera_ciclo_legado_e_intervalo_sem_execucoes(contexto, tmp_path):
    clock = Clock()
    db.session.add(ExecucaoColeta(estado="EM_ANDAMENTO", iniciada_em=clock.agora()))
    db.session.commit()
    clock.dormir(600)
    service, journal, repo, _ = montar(
        tmp_path, clock, SimpleNamespace(buscar_todas_posicoes=lambda: resposta(clock))
    )
    repo.restaurar(journal, 30, clock.agora())
    legado = ExecucaoColeta.query.one()
    assert legado.estado == "FALHA" and legado.erro == "PROCESSO_INTERROMPIDO"
    assert LacunaColeta.query.one().inicio == clock.inicio
    service.coletar_ciclo()
    assert RegistroBrutoGPS.query.count() == 1
    assert LacunaColeta.query.one().fim == clock.agora()


def test_99_porcento_controlado_nao_se_apresenta_como_mes_real(contexto, tmp_path):
    clock = Clock()
    i = [0]

    def buscar():
        i[0] += 1
        if i[0] == 50:
            raise SPTransHTTPError("/Posicao", 403)
        return resposta(clock)

    service, _, _, _ = montar(
        tmp_path, clock, SimpleNamespace(buscar_todas_posicoes=buscar)
    )
    for _ in range(100):
        service.coletar_ciclo(clock.agora())
        clock.dormir(30)
    r = medir_gravacao(clock.inicio, clock.agora())
    assert r["ciclos_observados"] == 100 and r["denominador"] == 100
    assert r["confirmados_ate_60s"] == 99 and r["percentual_ate_60s"] == 99
    assert r["meta_atingida"] and not r["observacao_mensal_comprovada"]


def test_caixa_interna_entrega_teste_sem_criar_lacuna_ou_gps(
    contexto, monkeypatch, capsys
):
    import sys
    from app.monitoramento import __main__ as cli

    monkeypatch.setattr(cli, "create_app", lambda: contexto)
    monkeypatch.setattr(
        db.engine, "dispose", lambda: None
    )  # manter o SQLite em memória entre chamadas CLI
    monkeypatch.setattr(sys, "argv", ["monitoramento", "teste-canal"])
    cli.main()
    enviado = json.loads(capsys.readouterr().out)
    assert enviado["simulado"] and enviado["tipo"] == "TESTE"
    assert NotificacaoColeta.query.one().lacuna_id is None
    assert LacunaColeta.query.count() == 0 and RegistroBrutoGPS.query.count() == 0
    monkeypatch.setattr(sys, "argv", ["monitoramento", "alertas"])
    cli.main()
    caixa = json.loads(capsys.readouterr().out)
    assert caixa["alertas"][0]["id"] == enviado["id"]
    assert caixa["sql_disponivel"]
    # Outro computador não possui os arquivos do worker, mas vê a caixa SQL.
    from pathlib import Path

    monkeypatch.setattr(
        cli,
        "diretorio_journal",
        lambda: Path(caixa["diretorio"]).parent / "outro-computador",
    )
    cli.main()
    compartilhada = json.loads(capsys.readouterr().out)
    assert compartilhada["alertas"][0]["id"] == enviado["id"]


def test_caixa_local_continua_legivel_com_banco_indisponivel(
    contexto, tmp_path, monkeypatch, capsys
):
    import sys
    from app.monitoramento import __main__ as cli
    from app.monitoramento.journal import publicar

    monkeypatch.setattr(cli, "create_app", lambda: contexto)
    monkeypatch.setattr(db.engine, "dispose", lambda: None)
    monkeypatch.setattr(cli, "diretorio_journal", lambda: tmp_path)
    publicar(
        tmp_path / "alertas" / "um.json",
        dict(id="um", criada_em="2026-11-19T15:00:00+00:00", tipo="FALHA"),
    )
    monkeypatch.setattr(
        cli,
        "listar_alertas",
        Mock(side_effect=OperationalError("sql", {}, Exception("offline"))),
    )
    monkeypatch.setattr(sys, "argv", ["monitoramento", "alertas"])
    cli.main()
    caixa = json.loads(capsys.readouterr().out)
    assert not caixa["sql_disponivel"] and caixa["alertas"][0]["id"] == "um"


def test_fim_do_ciclo_usa_confirmacao_e_nao_inicio_do_insert(
    contexto, tmp_path, monkeypatch
):
    clock = Clock()
    service, _, repo, _ = montar(
        tmp_path, clock, SimpleNamespace(buscar_todas_posicoes=lambda: resposta(clock))
    )
    original = repo.concluir

    def confirmar_depois(*args):
        total = original(*args)
        clock.dormir(7)
        return total

    monkeypatch.setattr(repo, "concluir", confirmar_depois)
    service.coletar_ciclo(clock.inicio)
    e = ExecucaoColeta.query.one()
    assert e.finalizada_em == e.confirmada_em == clock.agora()
    assert e.duracao_gravacao_ms == 7000


def test_intervalo_sem_ciclo_comeca_na_proxima_execucao_planejada(contexto, tmp_path):
    clock = Clock()
    db.session.add(
        ExecucaoColeta(
            fonte="OLHO_VIVO",
            estado="OK",
            prevista_em=clock.inicio,
            iniciada_em=clock.inicio,
            finalizada_em=clock.inicio + timedelta(seconds=20),
        )
    )
    db.session.commit()
    clock.dormir(35)
    _, journal, repo, _ = montar(
        tmp_path, clock, SimpleNamespace(buscar_todas_posicoes=lambda: resposta(clock))
    )
    repo.restaurar(journal, 30, clock.agora())
    assert LacunaColeta.query.one().inicio == clock.inicio + timedelta(seconds=30)
    assert RegistroBrutoGPS.query.count() == 0
