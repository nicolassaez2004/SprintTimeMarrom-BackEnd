"""US22-T01: GET /posicoes/atuais com dados SINTÉTICOS em SQLite."""

from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import event

from app import create_app, db
from app.consultas.posicoes import ultimas_posicoes
from app.models import (
    ArquivoArmazenado,
    ExecucaoColeta,
    Linha,
    ObservacaoTratada,
    RegistroBrutoGPS,
    Veiculo,
    VersaoGTFS,
)

AGORA = datetime(2026, 11, 19, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
def app():
    aplicacao = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://",
                            "MYSQL_SSL_CA": None})
    with aplicacao.app_context():
        event.listen(db.engine, "connect", lambda c, _: c.execute("PRAGMA foreign_keys=ON"))
        db.create_all()
        yield aplicacao
        db.session.remove()
        db.engine.dispose()


@pytest.fixture
def client(app):
    return app.test_client()


def bruto(prefixo, linha, segundos_atras, *, lat=-23.55, lon=-46.63, sentido="1",
          estado="OK", com_gps=True):
    coletado = AGORA - timedelta(seconds=segundos_atras)
    if db.session.get(Veiculo, prefixo) is None:
        db.session.add(Veiculo(prefixo=prefixo))
    execucao = ExecucaoColeta(iniciada_em=coletado, prevista_em=coletado, estado=estado)
    db.session.add(execucao)
    db.session.flush()
    registro = RegistroBrutoGPS(
        execucao_id=execucao.id, veiculo_prefixo=prefixo, prefixo_origem=prefixo,
        linha_origem=linha, sentido_origem=sentido, latitude=lat, longitude=lon,
        horario_gps=coletado - timedelta(seconds=5) if com_gps else None,
        coletado_em=coletado, payload={"p": prefixo},
    )
    db.session.add(registro)
    db.session.commit()
    return registro


def por_prefixo(resposta):
    return {v["prefixo"]: v for v in resposta["veiculos"]}


def test_sem_registros_responde_lista_vazia(client):
    resposta = client.get("/posicoes/atuais")
    assert resposta.status_code == 200
    corpo = resposta.get_json()
    assert corpo["total"] == 0 and corpo["veiculos"] == []
    assert corpo["ultima_coleta_ok_em"] is None


def test_retorna_ultima_posicao_de_cada_veiculo_com_horarios(app):
    bruto("11001", "100", 300, lat=-23.0)
    novo = bruto("11001", "100", 20, lat=-23.1, lon=-46.1)
    bruto("22002", "100", 40)
    corpo = ultimas_posicoes(agora=AGORA)
    veiculos = por_prefixo(corpo)
    assert corpo["total"] == 2
    assert veiculos["11001"]["latitude"] == -23.1
    assert veiculos["11001"]["longitude"] == -46.1
    assert veiculos["11001"]["coletado_em"] == novo.coletado_em.isoformat()
    assert veiculos["11001"]["horario_gps"] == novo.horario_gps.isoformat()
    assert veiculos["11001"]["linha"]["codigo_olho_vivo"] == "100"
    assert corpo["ultima_coleta_ok_em"] == (AGORA - timedelta(seconds=20)).isoformat()


def test_filtro_por_linha_nao_mistura_linhas(app):
    bruto("11001", "100", 600)   # estava na 100...
    bruto("11001", "200", 10)    # ...e agora está na 200
    bruto("33003", "100", 15)
    na_100 = por_prefixo(ultimas_posicoes(linha="100", agora=AGORA))
    na_200 = por_prefixo(ultimas_posicoes(linha="200", agora=AGORA))
    assert set(na_100) == {"33003"}
    assert set(na_200) == {"11001"}
    assert all(v["linha"]["codigo_olho_vivo"] == "100" for v in na_100.values())
    assert ultimas_posicoes(linha="999", agora=AGORA)["veiculos"] == []


def test_posicao_antiga_e_distinguivel_da_recente(app):
    bruto("11001", "100", 30)
    bruto("22002", "100", 600)
    veiculos = por_prefixo(ultimas_posicoes(agora=AGORA))
    assert veiculos["11001"]["recente"] is True
    assert veiculos["11001"]["idade_segundos"] == 30
    assert veiculos["22002"]["recente"] is False
    assert veiculos["22002"]["idade_segundos"] == 600


def test_registro_sem_coordenada_ou_gps_nao_substitui_ultima_posicao_conhecida(app):
    bruto("11001", "100", 120, lat=-23.2)
    bruto("11001", "100", 10, lat=None, lon=None)
    bruto("11001", "100", 5, com_gps=False)
    assert por_prefixo(ultimas_posicoes(agora=AGORA))["11001"]["latitude"] == -23.2


def test_posicao_fora_da_janela_de_consulta_nao_aparece(app):
    bruto("11001", "100", 25 * 3600)
    assert ultimas_posicoes(agora=AGORA)["veiculos"] == []


def test_filtro_por_linha_gtfs_usa_correspondencia_da_observacao_tratada(app):
    arquivo = ArquivoArmazenado(chave="gtfs/x.zip", nome_original="x.zip",
                                mime_type="application/zip", tamanho_bytes=0, sha256="a" * 64)
    versao = VersaoGTFS(hash="a" * 64, vigencia_inicio=date(2026, 1, 1), arquivo=arquivo)
    db.session.add(versao)
    db.session.flush()
    db.session.add(Linha(versao_gtfs_id=versao.id, codigo="8000-10", nome="Linha sintética"))
    ligado = bruto("11001", "100", 10)
    bruto("22002", "100", 10)
    db.session.add(ObservacaoTratada(bruto_id=ligado.id, valida=True, versao_correspondencia="t",
                                     versao_gtfs_id=versao.id, linha_codigo="8000-10",
                                     sentido_gtfs="0"))
    db.session.commit()
    corpo = ultimas_posicoes(linha_gtfs="8000-10", agora=AGORA)
    assert [v["prefixo"] for v in corpo["veiculos"]] == ["11001"]
    assert corpo["veiculos"][0]["linha"]["gtfs"] == {
        "codigo": "8000-10", "nome": "Linha sintética", "sentido": "0",
        "versao_gtfs_id": versao.id}
    sem_vinculo = por_prefixo(ultimas_posicoes(agora=AGORA))["22002"]
    assert sem_vinculo["linha"]["gtfs"] is None


def test_rota_le_o_banco_sem_chamar_sptrans(client):
    # Posição de 10 s atrás no relógio real, pois a rota usa o instante atual.
    bruto("11001", "100", AGORA.timestamp() - datetime.now(timezone.utc).timestamp() + 10)
    with patch("app.integrations.sptrans.SPTransClient.__init__",
               side_effect=AssertionError("não deve chamar a SPTrans")):
        resposta = client.get("/posicoes/atuais?linha=100")
    assert resposta.status_code == 200
    assert [v["prefixo"] for v in resposta.get_json()["veiculos"]] == ["11001"]
    assert client.get("/api/posicoes/atuais").status_code == 200


@pytest.mark.parametrize("consulta,campo", [
    ("linha=abc", "linha"), ("linha=", "linha"), ("linha=1&linha=2", "linha"),
    ("prefixo=1", "prefixo"), ("linha_gtfs=", "linha_gtfs"),
])
def test_rota_rejeita_filtros_invalidos(client, consulta, campo):
    resposta = client.get(f"/posicoes/atuais?{consulta}")
    assert resposta.status_code == 400
    assert resposta.get_json()["erro"]["campo"] == campo


def test_review_um_registro_por_veiculo_mesmo_com_empate_no_ciclo(app):
    """Consulta por índice (review): empate no mesmo coletado_em fica com o maior id."""
    primeiro = bruto("11001", "100", 10)
    segundo = RegistroBrutoGPS(
        execucao_id=primeiro.execucao_id, veiculo_prefixo="11001", prefixo_origem="11001",
        linha_origem="200", sentido_origem="2", latitude=-23.6, longitude=-46.7,
        horario_gps=primeiro.horario_gps - timedelta(seconds=1), coletado_em=primeiro.coletado_em)
    db.session.add(segundo)
    bruto("11002", "100", 4000)  # fora dos 60 s, ainda na janela de 24 h
    db.session.add(Veiculo(prefixo="sem-posicao"))
    db.session.commit()
    resposta = ultimas_posicoes(agora=AGORA)
    assert resposta["total"] == 2
    veiculos = por_prefixo(resposta)
    assert veiculos["11001"]["linha"]["codigo_olho_vivo"] == "200"  # maior id
    assert veiculos["11002"]["recente"] is False and veiculos["11002"]["idade_segundos"] == 4000

def test_mapa_le_apenas_ultimo_ciclo_que_gravou_posicoes(client):
    agora_real = datetime.now(timezone.utc)
    ajuste = AGORA.timestamp() - agora_real.timestamp()
    anterior = bruto("11001", "100", ajuste + 90)
    ultimo = bruto("22002", "200", ajuste + 60)
    bruto("33003", "300", ajuste + 30)  # ciclo repetido, sem novos registros.
    for registro in (anterior, ultimo):
        db.session.get(ExecucaoColeta, registro.execucao_id).novos_persistidos = 1
    db.session.add(RegistroBrutoGPS(
        execucao_id=ultimo.execucao_id, veiculo_prefixo="22002", prefixo_origem="22002",
        linha_origem="210", latitude=-23.55, longitude=-46.63,
        horario_gps=ultimo.horario_gps - timedelta(seconds=1), coletado_em=ultimo.coletado_em,
    ))
    db.session.commit()

    resposta = client.get("/posicoes/atuais?modo=ultima_coleta")
    assert resposta.status_code == 200
    veiculos = por_prefixo(resposta.get_json())
    assert set(veiculos) == {"22002"}
    assert veiculos["22002"]["linha"]["codigo_olho_vivo"] == "210"
    assert client.get("/posicoes/atuais?modo=ultima_coleta&linha=200").get_json()["total"] == 0
    assert client.get("/posicoes/atuais?modo=desconhecido").status_code == 400
