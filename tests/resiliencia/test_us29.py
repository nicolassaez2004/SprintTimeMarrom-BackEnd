import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import event
from sqlalchemy.exc import OperationalError

from app import create_app, db
from app.coleta.resiliencia import CicloResiliente
from app.coleta.retentativas import temporaria
from app.integrations.sptrans import (
    SPTransConexaoError,
    SPTransConfiguracaoError,
    SPTransHTTPError,
)
from app.models import ExecucaoColeta, LacunaColeta, NotificacaoColeta, RegistroBrutoGPS
from app.monitoramento.journal import JournalColeta
from app.monitoramento.metricas import medir_gravacao
from app.repositories.coleta import ColetaRepository, PossePerdida


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
    caixa = tmp_path / "checkpoint/alertas"
    assert len(list(caixa.glob("*.json"))) == 2
    assert {a.tipo for a in NotificacaoColeta.query.all()} == {"FALHA", "RECUPERACAO"}
    assert LacunaColeta.query.one().fim == clock.agora()
    # Já sincronizado e entregue: o checkpoint não acumula histórico.
    assert journal.dados["lacunas"] == [] and journal.dados["alertas"] == []
    journal.abrir_lacuna("FALHA_CURTA")
    clock.dormir(30)
    service.coletar_ciclo()
    assert len(list(caixa.glob("*.json"))) == 2 and LacunaColeta.query.count() == 2
    assert NotificacaoColeta.query.count() == 2


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


# --- Correções do review US29 -------------------------------------------------


def test_timeout_do_requests_e_temporario_com_causa_propria(contexto):
    import requests

    from app.coleta.retentativas import causa_segura
    from app.integrations.sptrans import SPTransClient, SPTransTimeoutError

    contexto.config.update(SPTRANS_TOKEN="segredo", SPTRANS_API_URL="http://h")
    sessao = Mock()
    sessao.request.side_effect = requests.exceptions.ReadTimeout(
        "http://h?token=segredo"
    )
    with pytest.raises(SPTransTimeoutError) as erro:
        SPTransClient(session=sessao)
    assert temporaria(erro.value) and "segredo" not in str(erro.value)
    assert causa_segura(erro.value) == "SPTransTimeoutError"


def test_resposta_invalida_da_fonte_e_retentada(contexto, tmp_path):
    from app.integrations.sptrans import SPTransRespostaInvalidaError

    clock = Clock()
    client = SimpleNamespace(
        buscar_todas_posicoes=Mock(
            side_effect=[SPTransRespostaInvalidaError("html"), resposta(clock)]
        )
    )
    service, _, _, factory = montar(tmp_path, clock, client)
    assert service.coletar_ciclo() == 1 and factory.call_count == 2


def test_falha_recuperada_antes_do_proximo_ciclo_nao_e_lacuna(contexto, tmp_path):
    clock = Clock()
    client = SimpleNamespace(
        buscar_todas_posicoes=Mock(
            side_effect=[SPTransConexaoError("x"), resposta(clock)]
        )
    )
    service, journal, _, _ = montar(tmp_path, clock, client)
    assert service.coletar_ciclo() == 1
    e = ExecucaoColeta.query.one()
    assert e.tentativas == 2 and e.lacuna_id is None
    assert LacunaColeta.query.count() == 0 and journal.dados["lacunas"] == []


def test_causas_distintas_acumulam_na_mesma_lacuna(contexto, tmp_path):
    from app.integrations.sptrans import SPTransTimeoutError

    clock = Clock()
    client = SimpleNamespace(
        buscar_todas_posicoes=Mock(
            side_effect=[SPTransTimeoutError("t")]
            + [SPTransHTTPError("/Posicao", 503)] * 5
        )
    )
    service, *_ = montar(tmp_path, clock, client)
    service.coletar_ciclo()
    lacuna = LacunaColeta.query.one()
    assert lacuna.causa == "SPTransTimeoutError;SPTRANS_HTTP_503"
    assert lacuna.inicio == clock.inicio and lacuna.fim is None


def test_checkpoint_nao_acumula_historico_de_lacunas(contexto, tmp_path):
    clock = Clock()
    falhar = [False]

    def buscar():
        if falhar[0]:
            raise SPTransHTTPError("/Posicao", 403)
        return resposta(clock)

    service, journal, _, _ = montar(
        tmp_path, clock, SimpleNamespace(buscar_todas_posicoes=buscar)
    )
    for i in range(60):
        falhar[0] = i % 2 == 0
        service.coletar_ciclo()
        clock.dormir(30)
    assert LacunaColeta.query.count() == 30
    assert journal.dados["lacunas"] == [] and journal.dados["alertas"] == []
    assert journal.caminho.stat().st_size < 2048


def test_alerta_conta_falha_ainda_em_retentativa_no_ciclo(tmp_path):
    clock = Clock()
    journal = JournalColeta(tmp_path, agora=clock.agora)
    journal.iniciar_ciclo()
    journal.atualizar_ciclo(
        erro="SPTransTimeoutError", falha_inicio=clock.agora().isoformat()
    )
    clock.dormir(599)
    journal.verificar_alerta()
    assert journal.dados["lacunas"] == []
    clock.dormir(1)
    journal.verificar_alerta()
    (lacuna,) = journal.dados["lacunas"]
    assert lacuna["inicio"] == clock.inicio.isoformat() and lacuna["fim"] is None
    assert [a["tipo"] for a in journal.dados["alertas"]] == ["FALHA"]
    assert journal.dados["ciclo"]["lacuna_id"] == lacuna["id"]


def test_webhook_so_marca_enviada_apos_confirmacao_e_reenvia(contexto, tmp_path):
    from app.integrations.alertas import AlertaEntregaError

    clock = Clock()
    notificar = Mock(side_effect=[AlertaEntregaError("HTTP 500"), None, None])
    notificar.canal = "WEBHOOK"
    journal = JournalColeta(
        tmp_path / "checkpoint", agora=clock.agora, notificar=notificar
    )
    repo = ColetaRepository()
    repo.assumir()
    service = CicloResiliente(
        journal,
        repo,
        lambda: SimpleNamespace(buscar_todas_posicoes=lambda: resposta(clock)),
        monotonic=clock.monotonic,
        dormir=clock.dormir,
    )
    journal.abrir_lacuna("SPTransTimeoutError")
    clock.dormir(600)
    journal.verificar_alerta()
    (alerta,) = journal.dados["alertas"]
    assert alerta["canal"] == "WEBHOOK" and alerta["enviada_em"] is None
    journal.entregar_pendentes()  # falha: reagenda em 2 s
    journal.entregar_pendentes()  # ainda não é hora
    assert notificar.call_count == 1 and alerta["tentativas_entrega"] == 1
    service.coletar_ciclo()  # recupera e sincroniza com entregas pendentes
    assert {n.enviada_em for n in NotificacaoColeta.query.all()} == {None}
    assert len(journal.dados["alertas"]) == 2  # pendentes não são podados
    clock.dormir(2)
    journal.entregar_pendentes()
    assert notificar.call_count == 3
    assert all(a["enviada_em"] for a in journal.dados["alertas"])
    clock.dormir(30)
    service.coletar_ciclo()
    notificacoes = NotificacaoColeta.query.all()
    assert {n.tipo for n in notificacoes} == {"FALHA", "RECUPERACAO"}
    assert all(n.enviada_em and n.canal == "WEBHOOK" for n in notificacoes)
    assert journal.dados["alertas"] == []


def test_adaptador_webhook_nao_expoe_url():
    import requests

    from app.integrations.alertas import AlertaEntregaError, AlertaWebhook

    url = "https://discord.example/api/webhooks/1/segredo"
    evento = dict(mensagem="Falha", inicio="i", fim=None, causa="c")
    sessao = Mock()
    sessao.post.return_value = SimpleNamespace(status_code=204)
    AlertaWebhook(url, session=sessao)(evento)
    corpo = sessao.post.call_args.kwargs["json"]
    assert corpo["content"] == corpo["text"] and "em aberto" in corpo["text"]
    assert sessao.post.call_args.kwargs["timeout"]
    sessao.post.return_value = SimpleNamespace(status_code=500)
    with pytest.raises(AlertaEntregaError):
        AlertaWebhook(url, session=sessao)(evento)
    sessao.post.side_effect = requests.ConnectionError(url)
    with pytest.raises(AlertaEntregaError) as erro:
        AlertaWebhook(url, session=sessao)(evento)
    assert "segredo" not in str(erro.value) and erro.value.__cause__ is None


def test_monitor_em_thread_alerta_e_entrega_sem_ciclo(tmp_path):
    import threading
    import time

    clock = Clock()
    notificar = Mock()
    notificar.canal = "WEBHOOK"
    journal = JournalColeta(tmp_path, agora=clock.agora, notificar=notificar)
    journal.abrir_lacuna("SPTransTimeoutError")
    clock.t = 600
    parar = threading.Event()
    monitor = threading.Thread(target=journal.vigiar, args=(parar,), daemon=True)
    monitor.start()
    time.sleep(1.3)
    parar.set()
    monitor.join()
    assert notificar.call_count == 1 and journal.dados["alertas"][0]["enviada_em"]


def test_monitor_sobrevive_a_erro_inesperado(tmp_path):
    import threading

    journal = JournalColeta(tmp_path)
    parar = threading.Event()
    chamadas = []

    def falhar():
        chamadas.append(1)
        if len(chamadas) == 2:
            parar.set()
        raise KeyError("bug")

    journal.verificar_alerta = falhar
    monitor = threading.Thread(target=journal.vigiar, args=(parar,), daemon=True)
    monitor.start()
    monitor.join(timeout=5)
    assert len(chamadas) == 2


def test_worker_continuo_retoma_apos_erro_inesperado_e_de_banco(contexto, monkeypatch):
    from app.coleta import bus_collector, resiliencia

    lock = SimpleNamespace(
        adquirir=lambda: True, ativo=lambda: True, liberar=lambda: None
    )
    monkeypatch.setattr(bus_collector, "LockColetor", lambda _: lock)
    monkeypatch.setattr(
        resiliencia.CicloResiliente,
        "coletar_ciclo",
        Mock(
            side_effect=[
                KeyError("bug"),
                OperationalError("sql", {}, Exception(2013, "perdida")),
                1,
            ]
        ),
    )
    esperas = []
    assert resiliencia.executar_worker(max_ciclos=1, dormir=esperas.append) == 0
    assert esperas[:2] == [1, 2]
    lacuna = LacunaColeta.query.one()
    assert "KeyError" in lacuna.causa and lacuna.fim is None


def test_segundo_processo_nao_usa_o_mesmo_checkpoint(contexto):
    import subprocess
    import sys

    from app.coleta.resiliencia import diretorio_journal, executar_worker

    pasta = diretorio_journal()
    pasta.mkdir(parents=True, exist_ok=True)
    outro = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys, time; from filelock import FileLock;"
            "l = FileLock(sys.argv[1]); l.acquire(); print('ok', flush=True);"
            "time.sleep(30)",
            str(pasta / "worker.lock"),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert outro.stdout.readline().strip() == "ok"
        assert executar_worker(max_ciclos=1) == 1
        assert ExecucaoColeta.query.count() == 0
    finally:
        outro.kill()
        outro.wait()


def test_erro_sql_de_configuracao_nao_e_tratado_como_lote_rejeitado(
    contexto, tmp_path, monkeypatch
):
    clock = Clock()
    service, journal, repo, _ = montar(
        tmp_path, clock, SimpleNamespace(buscar_todas_posicoes=lambda: resposta(clock))
    )
    monkeypatch.setattr(
        repo,
        "concluir",
        Mock(side_effect=OperationalError("sql", {}, Exception(1045, "negado"))),
    )
    with pytest.raises(OperationalError):
        service.coletar_ciclo()
    assert not (journal.diretorio / "rejeitados").exists()
    assert journal.dados["ciclo"]["resposta"] is not None  # preservada para retomada


def test_metrica_separa_falhas_da_fonte(contexto):
    inicio = Clock().inicio
    for i in range(100):
        erro = "SPTRANS_HTTP_503" if i < 2 else "OperationalError" if i == 2 else None
        db.session.add(
            ExecucaoColeta(
                fonte="OLHO_VIVO",
                prevista_em=inicio + timedelta(seconds=30 * i),
                iniciada_em=inicio + timedelta(seconds=30 * i),
                estado="FALHA" if erro else "OK",
                erro=erro,
                confirmada_em=None if erro else inicio + timedelta(seconds=30 * i + 5),
                duracao_gravacao_ms=None if erro else 5000,
            )
        )
    db.session.commit()
    r = medir_gravacao(inicio, inicio + timedelta(seconds=3000))
    assert r["percentual_ate_60s"] == 97 and not r["meta_atingida"]
    assert r["falhas_da_fonte"] == 2 and r["denominador_sem_falhas_da_fonte"] == 98
    assert r["percentual_ate_60s_sem_falhas_da_fonte"] == 98.98
    assert not r["meta_atingida_sem_falhas_da_fonte"]  # falha interna conta


@pytest.mark.parametrize("entregue", [True, False])
def test_teste_canal_usa_webhook_configurado(contexto, monkeypatch, capsys, entregue):
    import sys

    from app.integrations.alertas import AlertaEntregaError
    from app.monitoramento import __main__ as cli

    contexto.config["ALERTA_WEBHOOK_URL"] = "https://hook.example/segredo"
    enviar = Mock(side_effect=None if entregue else AlertaEntregaError("HTTP 500"))
    monkeypatch.setattr(cli, "AlertaWebhook", lambda url: enviar)
    monkeypatch.setattr(cli, "create_app", lambda: contexto)
    monkeypatch.setattr(db.engine, "dispose", lambda: None)
    monkeypatch.setattr(sys, "argv", ["monitoramento", "teste-canal"])
    if entregue:
        cli.main()
    else:
        with pytest.raises(SystemExit):
            cli.main()
    saida = capsys.readouterr().out
    assert "segredo" not in saida and json.loads(saida)["canal"] == "WEBHOOK"
    n = NotificacaoColeta.query.one()
    assert n.canal == "WEBHOOK" and (n.enviada_em is not None) is entregue
