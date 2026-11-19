"""US13-T01/T02: consolidação e GET /qualidade com dados SINTÉTICOS em SQLite."""

from datetime import date, datetime, time, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import event

from app import create_app, db
from app.models import ArquivoArmazenado, ExecucaoColeta, LacunaColeta, VersaoGTFS
from app.qualidade.service import FUSO, consolidar_qualidade

DIA = date(2026, 11, 18)
D0 = datetime.combine(DIA, time(), FUSO)  # meia-noite em São Paulo


def utc(instante):
    """Instantes saem em UTC, como gravados no banco."""
    return instante.astimezone(timezone.utc).isoformat()


@pytest.fixture
def app():
    aplicacao = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://",
                            "MYSQL_SSL_CA": None, "GPS_COLLECTION_INTERVAL_SECONDS": 30})
    with aplicacao.app_context():
        event.listen(db.engine, "connect", lambda c, _: c.execute("PRAGMA foreign_keys=ON"))
        db.create_all()
        yield aplicacao
        db.session.remove()
        db.engine.dispose()


@pytest.fixture
def client(app):
    return app.test_client()


def ciclos(indices, estado="OK", erro=None, lacuna=None, base=D0):
    for k in indices:
        instante = base + timedelta(seconds=30 * k)
        db.session.add(ExecucaoColeta(prevista_em=instante, iniciada_em=instante,
                                      estado=estado, erro=erro, lacuna_id=lacuna))
    db.session.commit()


def lacuna(inicio, fim, causa="SPTransTimeoutError"):
    registro = LacunaColeta(id=str(uuid4()), inicio=inicio, fim=fim, causa=causa,
                            fonte="OLHO_VIVO", aberta_fonte="OLHO_VIVO" if fim is None else None)
    db.session.add(registro)
    db.session.commit()
    return registro


def versao(inicio, fim, letra):
    arquivo = ArquivoArmazenado(chave=f"gtfs/{letra}.zip", nome_original=f"{letra}.zip",
                                mime_type="application/zip", tamanho_bytes=0, sha256=letra * 64)
    registro = VersaoGTFS(hash=letra * 64, vigencia_inicio=inicio, vigencia_fim=fim, arquivo=arquivo)
    db.session.add(registro)
    db.session.commit()
    return registro


def consolidar(agora, **kw):
    return consolidar_qualidade(kw.pop("inicio", DIA), kw.pop("fim", DIA), agora=agora,
                                intervalo=30, **kw)


def test_sem_coleta_nao_vira_zero_de_operacao(app):
    dia = consolidar(D0 + timedelta(days=1), incluir_janelas=True)["dias"][0]
    assert dia["situacao"] == "FORA_DO_MONITORAMENTO"
    assert dia["percentual_falhas"] is None and dia["incompleto"] is None
    assert dia["ciclos_esperados"] == 0 and dia["lacunas"] == []
    assert {j["situacao"] for j in dia["janelas"]} == {"FORA_DO_MONITORAMENTO"}


def test_janela_completa_e_futuro_pendente(app):
    ciclos(range(30))
    dia = consolidar(D0 + timedelta(minutes=15), incluir_janelas=True)["dias"][0]
    primeira, segunda = dia["janelas"][:2]
    assert primeira["ciclos_esperados"] == 30 and primeira["ciclos_com_dados"] == 30
    assert primeira["percentual_falhas"] == 0 and primeira["situacao"] == "COMPLETA"
    assert segunda["situacao"] == "PENDENTE" and segunda["percentual_falhas"] is None
    assert dia["situacao"] == "COMPLETA" and dia["lacunas"] == []


def test_janelas_de_15_minutos_no_dia_de_sao_paulo(app):
    ciclos([0])
    janelas = consolidar(D0 + timedelta(days=1), incluir_janelas=True)["dias"][0]["janelas"]
    assert len(janelas) == 96
    assert janelas[0]["inicio"] == "2026-11-18T03:00:00+00:00"
    assert janelas[0]["fim"] == "2026-11-18T03:15:00+00:00"
    assert janelas[-1]["fim"] == "2026-11-19T03:00:00+00:00"
    # Monitorada e sem nenhum ciclo: sem dados (100% de falha), nunca "zero ônibus".
    assert janelas[1]["situacao"] == "SEM_DADOS"
    assert janelas[1]["percentual_falhas"] == 100 and janelas[1]["incompleto"] is True


def test_falhas_e_lacunas_registradas_e_inferidas(app):
    registrada = lacuna(D0 + timedelta(seconds=600), D0 + timedelta(seconds=660))
    ciclos(range(10))
    ciclos([20, 21], estado="FALHA", erro="SPTRANS_HTTP_503", lacuna=registrada.id)
    ciclos(range(22, 30))
    corpo = consolidar(D0 + timedelta(minutes=15), incluir_janelas=True)
    dia = corpo["dias"][0]
    janela = dia["janelas"][0]
    assert janela["ciclos_esperados"] == 30 and janela["ciclos_registrados"] == 20
    assert janela["ciclos_falha"] == 2 and janela["falhas_da_fonte"] == 2
    assert janela["sem_registro"] == 10
    assert janela["percentual_falhas"] == 40.0 and janela["incompleto"] is True
    assert janela["situacao"] == "INCOMPLETA"
    assert [(l["origem"], l["causa"], l["inicio"], l["fim"], l["estado"]) for l in dia["lacunas"]] == [
        ("INFERIDA", "SEM_CICLO_REGISTRADO", utc(D0 + timedelta(seconds=300)),
         utc(D0 + timedelta(seconds=600)), "ENCERRADA"),
        ("REGISTRADA", "SPTransTimeoutError", registrada.inicio.isoformat(),
         registrada.fim.isoformat(), "ENCERRADA"),
    ]
    assert corpo["resumo"]["lacunas_encerradas"] == 2 and corpo["resumo"]["lacunas_abertas"] == 0


def test_processo_parado_gera_lacuna_aberta_sem_sobrepor_a_registrada(app):
    ciclos(range(10))  # último ciclo em 00:04:30
    lacuna(D0 + timedelta(minutes=5), D0 + timedelta(minutes=8), causa="PROCESSO_INTERROMPIDO")
    corpo = consolidar(D0 + timedelta(minutes=15))
    lacunas = corpo["dias"][0]["lacunas"]
    assert [(l["origem"], l["inicio"], l["fim"], l["estado"]) for l in lacunas] == [
        ("REGISTRADA", utc(D0 + timedelta(minutes=5)),
         utc(D0 + timedelta(minutes=8)), "ENCERRADA"),
        ("INFERIDA", utc(D0 + timedelta(minutes=8)), None, "ABERTA"),
    ]
    assert corpo["resumo"]["lacunas_abertas"] == 1


def test_lacuna_registrada_aberta_aparece_em_todos_os_dias_que_atinge(app):
    ciclos([0])
    aberta = lacuna(D0 + timedelta(seconds=30), None)
    ciclos(range(1, 2 * 2880), estado="FALHA", erro="SPTRANS_HTTP_503", lacuna=aberta.id)
    corpo = consolidar(D0 + timedelta(days=2), fim=DIA + timedelta(days=1))
    for dia in corpo["dias"]:
        assert [(l["id"], l["estado"], l["fim"]) for l in dia["lacunas"]] == [(aberta.id, "ABERTA", None)]
    assert corpo["resumo"]["lacunas_abertas"] == 1
    assert corpo["dias"][1]["situacao"] == "SEM_DADOS"


def test_versao_gtfs_historica_de_cada_dia(app):
    antiga = versao(date(2026, 1, 1), date(2026, 11, 17), "a")
    nova = versao(date(2026, 11, 18), None, "b")
    dias = consolidar(D0 + timedelta(days=1), inicio=DIA - timedelta(days=1))["dias"]
    assert [(d["data"], d["gtfs"]["situacao"], d["gtfs"]["versao"]["id"]) for d in dias] == [
        ("2026-11-17", "VIGENTE", antiga.id), ("2026-11-18", "VIGENTE", nova.id)]
    assert dias[1]["gtfs"]["versao"]["hash"] == "b" * 64


def test_versao_gtfs_ausente_ou_ambigua_nao_e_escolhida_em_silencio(app):
    a = versao(date(2026, 11, 18), None, "a")
    b = versao(date(2026, 11, 18), date(2026, 11, 30), "b")
    dias = consolidar(D0 + timedelta(days=1), inicio=DIA - timedelta(days=1))["dias"]
    assert dias[0]["gtfs"] == {"situacao": "SEM_VERSAO_VIGENTE", "versao": None, "candidatas": []}
    assert dias[1]["gtfs"]["situacao"] == "AMBIGUA" and dias[1]["gtfs"]["versao"] is None
    assert {v["id"] for v in dias[1]["gtfs"]["candidatas"]} == {a.id, b.id}


def test_rota_publica_retorna_dias_solicitados(client):
    hoje = datetime.now(timezone.utc).astimezone(FUSO).date()
    inicio = hoje - timedelta(days=2)
    resposta = client.get(f"/qualidade?inicio={inicio}&fim={hoje}")
    assert resposta.status_code == 200
    corpo = resposta.get_json()
    assert [d["data"] for d in corpo["dias"]] == [str(inicio + timedelta(days=i)) for i in range(3)]
    assert corpo["lacunas"] == [] and corpo["resumo"]["percentual_falhas"] is None
    assert "janelas" not in corpo["dias"][0]
    com_janelas = client.get(f"/api/qualidade?inicio={hoje}&janelas=true").get_json()
    assert len(com_janelas["dias"]) == 1 and len(com_janelas["dias"][0]["janelas"]) == 96


@pytest.mark.parametrize("consulta,campo", [
    ("", "inicio"), ("inicio=2026-13-01", "inicio"), ("inicio=18/11/2026", "inicio"),
    ("inicio=2026-11-18&fim=2026-11-17", "fim"), ("inicio=2026-11-18&fim=2099-01-01", "fim"),
    ("inicio=2026-09-01&fim=2026-11-18", "fim"), ("inicio=2026-11-18&inicio=2026-11-17", "inicio"),
    ("inicio=2026-11-18&janelas=sim", "janelas"), ("inicio=2026-11-18&linha=1", "linha"),
])
def test_rota_rejeita_parametros_invalidos(client, consulta, campo):
    resposta = client.get(f"/qualidade?{consulta}")
    assert resposta.status_code == 400
    assert resposta.get_json()["erro"]["campo"] == campo
