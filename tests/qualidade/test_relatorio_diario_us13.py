"""US13-T03: falhas conhecidas em dados sintéticos, sem acessar a Aiven."""

from datetime import date, datetime, timedelta

import pytest

from app import create_app, db
from app.models import ArquivoArmazenado, ExecucaoColeta, LacunaColeta, VersaoGTFS
from app.qualidade.service import FUSO

PRIMEIRO = date(2026, 11, 18)
SEGUNDO = date(2026, 11, 19)
TERCEIRO = date(2026, 11, 20)


def instante(dia, hora=0, minuto=0, segundo=0):
    return datetime(dia.year, dia.month, dia.day, hora, minuto, segundo, tzinfo=FUSO)


def criar_cenario():
    """Reutilizado pelo teste HTTP e pela demonstração isolada do navegador."""
    for letra, inicio, fim in [("a", PRIMEIRO, PRIMEIRO), ("b", SEGUNDO, None),
                                ("c", TERCEIRO, TERCEIRO)]:
        arquivo = ArquivoArmazenado(id=f"arquivo-{letra}", chave=f"gtfs/{letra}.zip",
                                    nome_original=f"{letra}.zip", mime_type="application/zip",
                                    tamanho_bytes=0, sha256=letra * 64)
        db.session.add(VersaoGTFS(id=f"gtfs-{letra}", arquivo=arquivo, hash=letra * 64,
                                  capturada_em=instante(inicio, 8),
                                  vigencia_inicio=inicio, vigencia_fim=fim))
    db.session.add(LacunaColeta(id="falha-meia-noite", inicio=instante(PRIMEIRO, 23, 50),
                                fim=instante(SEGUNDO, 0, 20), causa="SPTRANS_HTTP_503",
                                fonte="OLHO_VIVO"))
    for quando, estado in [(instante(PRIMEIRO, 23, 49, 30), "OK"),
                           (instante(PRIMEIRO, 23, 50), "FALHA"),
                           (instante(SEGUNDO), "FALHA"),
                           (instante(SEGUNDO, 0, 10), "FALHA"),
                           (instante(SEGUNDO, 0, 20), "OK"),
                           (instante(SEGUNDO, 0, 20, 30), "OK")]:
        db.session.add(ExecucaoColeta(iniciada_em=quando, prevista_em=quando,
                                      estado=estado, finalizada_em=quando,
                                      erro="SPTRANS_HTTP_503" if estado == "FALHA" else None))
    db.session.commit()


@pytest.fixture
def application(monkeypatch):
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://",
                      "MYSQL_SSL_CA": None, "GPS_COLLECTION_INTERVAL_SECONDS": 30})
    monkeypatch.setattr("app.qualidade.routes.agora_utc", lambda: instante(TERCEIRO + timedelta(days=1)))
    with app.app_context():
        db.create_all()
        criar_cenario()
        yield app
        db.session.remove()
        db.engine.dispose()


def test_api_recorta_falha_na_virada_e_preserva_evento_e_gtfs(application):
    resposta = application.test_client().get("/api/qualidade?inicio=2026-11-18&fim=2026-11-19&janelas=true")
    assert resposta.status_code == 200
    corpo = resposta.get_json()
    a, b = corpo["dias"]
    la = next(l for l in a["lacunas"] if l["id"] == "falha-meia-noite")
    lb = next(l for l in b["lacunas"] if l["id"] == "falha-meia-noite")
    assert la["inicio_no_dia"] == "2026-11-19T02:50:00+00:00"
    assert la["fim_no_dia"] == lb["inicio_no_dia"] == "2026-11-19T03:00:00+00:00"
    assert lb["fim_no_dia"] == "2026-11-19T03:20:00+00:00"
    assert la["inicio"] == lb["inicio"] and la["fim"] == lb["fim"]
    assert la["continua_no_dia_seguinte"] and lb["continua_do_dia_anterior"]
    assert la["causa"] == lb["causa"] == "SPTRANS_HTTP_503"
    assert a["gtfs"]["versao"]["id"] == "gtfs-a"
    assert b["gtfs"]["versao"]["id"] == "gtfs-b"
    assert len(a["janelas"]) == len(b["janelas"]) == 96


def test_api_dia_inteiro_sem_ciclos_nao_omite_silencio_nem_ambiguidade(application):
    corpo = application.test_client().get("/qualidade?inicio=2026-11-20&janelas=true").get_json()
    dia = corpo["dias"][0]
    assert dia["situacao"] == "SEM_DADOS" and dia["percentual_falhas"] == 100
    assert dia["ciclos_registrados"] == 0 and dia["ciclos_esperados"] == 2880
    assert {j["situacao"] for j in dia["janelas"]} == {"SEM_DADOS"}
    assert dia["lacunas"] and dia["lacunas"][0]["origem"] == "INFERIDA"
    assert dia["lacunas"][0]["inicio_no_dia"] == "2026-11-20T03:00:00+00:00"
    assert dia["lacunas"][0]["fim_no_dia"] == "2026-11-21T03:00:00+00:00"
    assert dia["lacunas"][0]["fim"] is None
    assert dia["gtfs"]["situacao"] == "AMBIGUA" and dia["gtfs"]["versao"] is None
    assert {v["id"] for v in dia["gtfs"]["candidatas"]} == {"gtfs-b", "gtfs-c"}


def test_antes_do_monitoramento_mantem_percentual_desconhecido(application):
    dia = application.test_client().get("/qualidade?inicio=2026-11-17&janelas=true").get_json()["dias"][0]
    assert dia["situacao"] == "FORA_DO_MONITORAMENTO"
    assert dia["percentual_falhas"] is None
    assert dia["gtfs"]["situacao"] == "SEM_VERSAO_VIGENTE"
    assert len(dia["janelas"]) == 96


def test_lacuna_aberta_do_dia_corrente_termina_no_instante_observado(application, monkeypatch):
    monkeypatch.setattr("app.qualidade.routes.agora_utc", lambda: instante(TERCEIRO, 10, 30))
    dia = application.test_client().get("/qualidade?inicio=2026-11-20&janelas=true").get_json()["dias"][0]
    lacuna = dia["lacunas"][0]
    assert lacuna["fim_no_dia"] == "2026-11-20T13:30:00+00:00"
    assert lacuna["estado"] == "ABERTA" and lacuna["fim"] is None
    assert not lacuna["continua_no_dia_seguinte"]
    assert dia["janelas"][-1]["situacao"] == "PENDENTE"
