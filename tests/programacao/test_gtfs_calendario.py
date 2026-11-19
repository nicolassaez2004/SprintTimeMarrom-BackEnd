"""Calendário GTFS, exceções e saídas após meia-noite (US04-T02A)."""

import io
import zipfile
from datetime import date, datetime, timezone

import pytest

from app import db
from app.gtfs.service import GTFSValidationError, capturar_gtfs
from app.models import ImportacaoProgramacao, Linha, PartidaProgramada
from app.programacao.gtfs import servicos_ativos
from app.programacao.service import gerar_programacao_gtfs

DATA = date(2026, 11, 19)  # quinta-feira
AGORA = datetime(2026, 11, 19, 12, tzinfo=timezone.utc)


class ClienteFeed:
    def __init__(self, conteudo):
        self.conteudo = conteudo

    def baixar_para(self, url, destino):
        destino.write(self.conteudo)


def pacote_gtfs(*, calendario=True, calendario_dates=True, horarios=None):
    horarios = horarios or {
        "trip-weekday": "25:10:00",
        "trip-added": "08:00:00",
        "trip-removed": "09:00:00",
        "trip-saturday": "10:00:00",
        "trip-volta": "05:00:00",
    }
    conteudos = {
        "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\na,SPTrans,https://sptrans.com.br,America/Sao_Paulo\n",
        "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\ns1,Inicial,-23.5,-46.6\n",
        "routes.txt": "route_id,route_short_name,route_long_name,route_type\n3762-10,3762-10,Linha teste,3\n",
        "trips.txt": (
            "route_id,service_id,trip_id,direction_id\n"
            "3762-10,weekday,trip-weekday,0\n"
            "3762-10,added,trip-added,0\n"
            "3762-10,removed,trip-removed,0\n"
            "3762-10,saturday,trip-saturday,0\n"
            "3762-10,weekday,trip-volta,1\n"
        ),
        "stop_times.txt": "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n",
    }
    conteudos["stop_times.txt"] += "".join(
        f"{trip},,,s1,2\n{trip},{horario},{horario},s1,1\n"
        for trip, horario in horarios.items()
    )
    if calendario:
        conteudos["calendar.txt"] = (
            "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
            "weekday,1,1,1,1,1,0,0,20261118,20261231\n"
            "removed,1,1,1,1,1,0,0,20261118,20261231\n"
            "saturday,0,0,0,0,0,1,0,20261118,20261231\n"
        )
    if calendario_dates:
        conteudos["calendar_dates.txt"] = (
            "service_id,date,exception_type\n"
            "added,20261119,1\n"
            "removed,20261119,2\n"
        )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as arquivo:
        for nome, conteudo in conteudos.items():
            arquivo.writestr(nome, conteudo)
        arquivo.writestr(
            "feed_info.txt",
            "feed_publisher_name,feed_publisher_url,feed_lang,feed_start_date,feed_end_date\n"
            "SPTrans,https://www.sptrans.com.br,pt,20261118,20261231\n",
        )
    return buffer.getvalue()


@pytest.fixture
def feed(app, tmp_path):
    # Esta captura sintética fornece o ZIP completo e fixa a versão do gerador.
    conteudo = pacote_gtfs()
    nova = capturar_gtfs(
        url="https://gtfs.exemplo/feed.zip", cliente=ClienteFeed(conteudo),
        diretorio=tmp_path / "feed-gtfs", agora=AGORA,
    )
    db.session.add(Linha(versao_gtfs_id=nova.id, codigo="3762-10", nome="Linha teste"))
    db.session.commit()
    return nova


def test_excecao_de_feriado_adiciona_e_remove_servicos(feed, tmp_path, app):
    # Usa o ZIP imutável capturado e verifica inclusividade dos limites e exceções.
    from app.gtfs.service import _abrir_versao

    with app.app_context():
        versao, arquivo = _abrir_versao(feed.id, tmp_path / "feed-gtfs")
        with arquivo:
            ativos = servicos_ativos(arquivo, DATA)
    assert ativos == {"weekday", "added"}


def test_25_10_preserva_data_operacional_e_excecoes_afetam_importacao(feed, tmp_path, app):
    with app.app_context():
        resultado = gerar_programacao_gtfs(
            "3762-10", "ida", DATA, "FERIADO", versao_gtfs_id=feed.id,
            diretorio=tmp_path / "feed-gtfs", agora=AGORA,
        )
        assert resultado["estado"] == "IMPORTADA"
        assert resultado["procedencia"]["fonte"] == "GTFS"
        assert resultado["procedencia"]["url"] is None
        assert resultado["procedencia"]["referencia"] == f"urn:sha256:{feed.hash}"
        assert resultado["procedencia"]["versao_gtfs_id"] == feed.id
        assert resultado["quantidade"] == 2
        saidas = [partida["saida_prevista"] for partida in resultado["partidas"]]
        assert saidas == ["2026-11-19T11:00:00+00:00", "2026-11-20T04:10:00+00:00"]
        item = ImportacaoProgramacao.query.one()
        assert item.data_operacao == DATA
        assert item.versao_fonte == feed.hash
        assert item.arquivo.sha256 == feed.hash

        repetida = gerar_programacao_gtfs(
            "3762-10", "0", DATA, "FERIADO", versao_gtfs_id=feed.id,
            diretorio=tmp_path / "feed-gtfs", agora=AGORA,
        )
        assert repetida["estado"] == "REPETIDA"
        assert ImportacaoProgramacao.query.count() == 1
        assert PartidaProgramada.query.count() == 2


def test_calendar_dates_sem_calendar_permitem_servico_adicionado(tmp_path):
    arquivo_bytes = pacote_gtfs(calendario=False)
    caminho = tmp_path / "somente-excecoes.zip"
    caminho.write_bytes(arquivo_bytes)
    with zipfile.ZipFile(caminho) as arquivo:
        ativos = servicos_ativos(arquivo, DATA)
    assert ativos == {"added"}


def test_geracao_filtra_o_sentido_gtfs_escolhido(feed, tmp_path, app):
    with app.app_context():
        resultado = gerar_programacao_gtfs(
            "3762-10", "volta", DATA, "FERIADO", versao_gtfs_id=feed.id,
            diretorio=tmp_path / "feed-gtfs", agora=AGORA,
        )
    assert resultado["quantidade"] == 1
    assert resultado["sentido"] == "1"
    assert resultado["partidas"][0]["saida_prevista"] == "2026-11-19T08:00:00+00:00"


@pytest.mark.parametrize("horario", ["25:10", "100:10:00", "24:60:00", "01:02:60"])
def test_horario_gtfs_invalido_e_rejeitado(horario):
    from app.programacao.gtfs import _segundos_gtfs

    with pytest.raises(GTFSValidationError):
        _segundos_gtfs(horario, 2)


def test_horario_acima_de_24_converte_em_segundos_de_servico():
    from app.programacao.gtfs import _segundos_gtfs

    assert _segundos_gtfs("25:10:00", 2) == 90_600


def test_consulta_http_recupera_partidas_persistidas_pelo_gerador_gtfs(feed, tmp_path, app):
    gerada = gerar_programacao_gtfs(
        "3762-10", "ida", DATA, "FERIADO", versao_gtfs_id=feed.id,
        diretorio=tmp_path / "feed-gtfs", agora=AGORA,
    )
    resposta = app.test_client().get("/partidas", query_string={
        "linha": "3762-10", "sentido": "ida", "data": DATA.isoformat(),
    })
    assert resposta.status_code == 200
    assert resposta.get_json()["partidas"] == gerada["partidas"]
    assert resposta.get_json()["procedencia"] == gerada["procedencia"]
    assert resposta.get_json()["tipo_dia"] == "FERIADO"
