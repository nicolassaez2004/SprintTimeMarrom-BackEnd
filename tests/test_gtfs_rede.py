"""Importação da rede GTFS (US02-T02A/T02B) em banco SQLite isolado."""

import io
import zipfile
from decimal import Decimal

import pytest
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError

from app import create_app, db
from app.gtfs.service import (
    GTFSValidationError, _indices_no_tracado, capturar_gtfs, importar_linhas_paradas,
    importar_trechos,
)
from app.models import LineString, Linha, Parada, Trecho, VersaoGTFS

ROUTES = (
    "route_id,agency_id,route_short_name,route_long_name,route_type\n"
    "1012-10,1,1012-10,Term. Jd. Britânia - Jd. Monte Belo,3\n"
    "175T-10,1,175T-10,Metrô Santana - Metrô Jabaquara,3\n"
)
STOPS = (
    "stop_id,stop_name,stop_desc,stop_lat,stop_lon\n"
    "18848,Clínicas,,-23.554022,-46.671108\n"
    "18849,Paulista B/C,,-23.555432,-46.662335\n"
    "18850,Consolação,,-23.557111,-46.660111\n"
)


@pytest.fixture
def app(tmp_path):
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://",
                      "GTFS_STORAGE_DIR": None})
    app.instance_path = str(tmp_path / "instance")
    with app.app_context():
        # O SQLite só aplica chaves estrangeiras com este PRAGMA.
        event.listen(db.engine, "connect",
                     lambda conexao, _: conexao.execute("PRAGMA foreign_keys=ON"))
        db.create_all()
        yield app
        db.session.remove()
        db.engine.dispose()


def _zip(routes=ROUTES, stops=STOPS, **extras):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as arquivo:
        arquivos = {
            "agency.txt": "agency_id,agency_name\n1,SPTRANS\n",
            "routes.txt": routes, "stops.txt": stops,
            "trips.txt": "route_id,service_id,trip_id\n",
            "stop_times.txt": "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n",
            "calendar.txt": "service_id\nUSD\n",
        }
        arquivos.update({f"{nome}.txt": conteudo for nome, conteudo in extras.items()})
        for nome, conteudo in arquivos.items():
            arquivo.writestr(nome, conteudo.encode("utf-8"))
    return buffer.getvalue()


class ClienteFalso:
    def __init__(self, conteudo):
        self.conteudo = conteudo

    def baixar_para(self, url, destino):
        destino.write(self.conteudo)


def _versao(conteudo=None):
    return capturar_gtfs(url="https://gtfs.exemplo/feed.zip",
                         cliente=ClienteFalso(conteudo or _zip()))


def test_importa_linhas_e_paradas_preservando_codigos_nomes_e_coordenadas(app):
    versao = _versao()
    assert importar_linhas_paradas(versao.id) == {"linhas": 2, "paradas": 3}

    linha = db.session.get(Linha, (versao.id, "1012-10"))
    assert linha.nome == "Term. Jd. Britânia - Jd. Monte Belo"
    assert linha.versao_gtfs.hash == versao.hash
    parada = db.session.get(Parada, (versao.id, "18849"))
    assert parada.nome == "Paulista B/C"
    assert (parada.latitude, parada.longitude) == (Decimal("-23.555432"),
                                                   Decimal("-46.662335"))
    assert {p.versao_gtfs_id for p in Parada.query} == {versao.id}


def test_reimportar_a_mesma_versao_nao_duplica(app):
    versao = _versao()
    importar_linhas_paradas(versao.id)
    assert importar_linhas_paradas(versao.id) == {"linhas": 2, "paradas": 3}
    assert Linha.query.count() == 2 and Parada.query.count() == 3


def test_codigos_repetidos_em_versoes_diferentes_ficam_separados(app):
    antiga = _versao()
    nova = _versao(_zip(
        routes=ROUTES.replace("Jd. Monte Belo", "Jd. Monte Belo (novo)"),
        stops=STOPS.replace("-23.554022", "-23.554100"),
    ))
    importar_linhas_paradas(antiga.id)
    importar_linhas_paradas(nova.id)

    assert Linha.query.filter_by(codigo="1012-10").count() == 2
    assert db.session.get(Linha, (antiga.id, "1012-10")).nome.endswith("Monte Belo")
    assert db.session.get(Linha, (nova.id, "1012-10")).nome.endswith("(novo)")
    assert db.session.get(Parada, (antiga.id, "18848")).latitude == Decimal("-23.554022")
    assert db.session.get(Parada, (nova.id, "18848")).latitude == Decimal("-23.5541")


@pytest.mark.parametrize("routes,stops,mensagem", [
    (ROUTES + "1012-10,1,1012-10,Duplicada,3\n", STOPS, "route_id '1012-10' repetido"),
    (ROUTES, STOPS + "18848,Clínicas 2,,-23.5,-46.6\n", "stop_id '18848' repetido"),
    (ROUTES, STOPS + "9,Sem lat,,,-46.6\n", "stop_lat inválida"),
])
def test_arquivo_invalido_ou_duplicado_nao_grava_nada(app, routes, stops, mensagem):
    versao = _versao(_zip(routes=routes, stops=stops))
    with pytest.raises(GTFSValidationError, match=mensagem):
        importar_linhas_paradas(versao.id)
    assert Linha.query.count() == Parada.query.count() == 0


def test_rejeita_versao_inexistente_no_servico_e_no_banco(app):
    with pytest.raises(LookupError, match="inexistente"):
        importar_linhas_paradas("00000000-0000-0000-0000-000000000000")

    db.session.add(Linha(versao_gtfs_id="nao-existe", codigo="1012-10", nome="X"))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()
    db.session.add(Parada(versao_gtfs_id="nao-existe", codigo="1", nome="X",
                          latitude=0, longitude=0))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()
    assert Linha.query.count() == Parada.query.count() == 0


def test_mesmo_codigo_na_mesma_versao_e_rejeitado_pelo_banco(app):
    versao = _versao()
    importar_linhas_paradas(versao.id)
    db.session.add(Linha(versao_gtfs_id=versao.id, codigo="1012-10", nome="Outra"))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_versao_com_linhas_nao_pode_ser_apagada(app):
    versao = _versao()
    importar_linhas_paradas(versao.id)
    with pytest.raises(IntegrityError):
        db.session.execute(VersaoGTFS.__table__.delete())
    db.session.rollback()
    assert VersaoGTFS.query.count() == 1


# --- US02-T02B: trechos -----------------------------------------------------
# Linha 1012-10 com paradas A-B-C-D em linha reta (oeste), de 0,01° em 0,01°.
PARADAS_LINHA = (
    "stop_id,stop_name,stop_lat,stop_lon\n"
    "A,Parada A,-23.5,-46.60\nB,Parada B,-23.5,-46.61\n"
    "C,Parada C,-23.5,-46.62\nD,Parada D,-23.5,-46.63\n"
)
# V1 e V2: mesma sequência (um trajeto só). V3: variante curta A-B-C.
# V4: volta D-C-B-A, sem shape.
TRIPS = (
    "route_id,service_id,trip_id,direction_id,shape_id\n"
    "1012-10,USD,V2,0,S0\n1012-10,USD,V1,0,S0\n"
    "1012-10,USD,V3,0,S0\n1012-10,USD,V4,1,\n"
)
# Fora de ordem e com stop_sequence não contíguo, como o GTFS permite.
STOP_TIMES = "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n" + "".join(
    f"{viagem},06:00:00,06:00:00,{parada},{sequencia}\n"
    for viagem, paradas in {"V1": "ABCD", "V2": "ABCD", "V3": "ABC", "V4": "DCBA"}.items()
    for parada, sequencia in reversed(list(zip(paradas, (1, 2, 5, 10))))
)
SHAPES = "shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence\n" + "".join(
    f"S0,-23.5,{-46.6 - 0.005 * i:.3f},{i + 1}\n" for i in reversed(range(7))
)


def _linha_exemplo(**alteracoes):
    arquivos = {"stops": PARADAS_LINHA, "trips": TRIPS,
                "stop_times": STOP_TIMES, "shapes": SHAPES} | alteracoes
    versao = _versao(_zip(stops=arquivos.pop("stops"), **arquivos))
    importar_linhas_paradas(versao.id)
    return versao


def _trajeto(versao, trajeto):
    return [(t.ordem, t.parada_origem_codigo, t.parada_destino_codigo)
            for t in Trecho.query.filter_by(versao_gtfs_id=versao.id, trajeto=trajeto)
            .order_by(Trecho.ordem)]


def test_trechos_preservam_ordem_sentido_e_trajetos_sem_misturar(app):
    versao = _linha_exemplo()
    assert importar_trechos(versao.id) == {"trechos": 3 + 2 + 3}

    # V2 tem a mesma sequência de V1 e não gera trechos repetidos.
    assert {t.trajeto for t in Trecho.query} == {"V1", "V3", "V4"}
    assert _trajeto(versao, "V1") == [(1, "A", "B"), (2, "B", "C"), (3, "C", "D")]
    assert _trajeto(versao, "V3") == [(1, "A", "B"), (2, "B", "C")]
    assert _trajeto(versao, "V4") == [(1, "D", "C"), (2, "C", "B"), (3, "B", "A")]
    assert {t.sentido for t in Trecho.query.filter_by(trajeto="V1")} == {"0"}
    assert {t.sentido for t in Trecho.query.filter_by(trajeto="V4")} == {"1"}


def test_trechos_ligam_linha_paradas_versao_e_geometria(app):
    versao = _linha_exemplo()
    importar_trechos(versao.id)
    ida = Trecho.query.filter_by(trajeto="V1", ordem=1).one()

    assert ida.linha.nome == "Term. Jd. Britânia - Jd. Monte Belo"
    assert (ida.origem.nome, ida.destino.nome) == ("Parada A", "Parada B")
    assert ida.linha.versao_gtfs.hash == versao.hash == ida.origem.versao_gtfs.hash
    # Recorte do shape entre as duas paradas, com o ponto intermediário.
    assert ida.geometria == [[-46.6, -23.5], [-46.605, -23.5], [-46.61, -23.5]]
    # Sem shape: reta entre as paradas.
    volta = Trecho.query.filter_by(trajeto="V4", ordem=1).one()
    assert volta.geometria == [[-46.63, -23.5], [-46.62, -23.5]]


def test_reimportar_trechos_nao_duplica(app):
    versao = _linha_exemplo()
    importar_trechos(versao.id)
    assert importar_trechos(versao.id) == {"trechos": 8}
    assert Trecho.query.count() == 8


@pytest.mark.parametrize("alteracao,mensagem", [
    ({"stop_times": STOP_TIMES + "V1,07:00:00,07:00:00,X,11\n"}, "parada inexistente"),
    ({"stop_times": STOP_TIMES + "V9,07:00:00,07:00:00,A,1\n"}, "viagem inexistente"),
    ({"trips": TRIPS + "9999-10,USD,V9,0,\n"}, "linha inexistente"),
    ({"stop_times": STOP_TIMES + "V1,07:00:00,07:00:00,D,5\n"}, "stop_sequence repetido"),
    ({"trips": TRIPS.replace("V4,1,", "V4,,")}, "direction_id vazio"),
])
def test_referencias_invalidas_no_feed_nao_gravam_trechos(app, alteracao, mensagem):
    versao = _linha_exemplo(**alteracao)
    with pytest.raises(GTFSValidationError, match=mensagem):
        importar_trechos(versao.id)
    assert Trecho.query.count() == 0


def test_trechos_exigem_linhas_e_paradas_importadas(app):
    versao = _versao(_zip(stops=PARADAS_LINHA, trips=TRIPS, stop_times=STOP_TIMES))
    with pytest.raises(GTFSValidationError, match="importe linhas e paradas"):
        importar_trechos(versao.id)
    with pytest.raises(LookupError, match="inexistente"):
        importar_trechos("nao-existe")


@pytest.mark.parametrize("campos", [
    {"parada_destino_codigo": "X"},
    {"parada_origem_codigo": "X"},
    {"linha_codigo": "9999-10"},
])
def test_banco_rejeita_trecho_com_referencia_inexistente(app, campos):
    versao = _linha_exemplo()
    dados = {"versao_gtfs_id": versao.id, "linha_codigo": "1012-10", "sentido": "0",
             "trajeto": "manual", "ordem": 1, "parada_origem_codigo": "A",
             "parada_destino_codigo": "B",
             "geometria": [[-46.6, -23.5], [-46.61, -23.5]]} | campos
    db.session.add(Trecho(**dados))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_banco_rejeita_trecho_com_parada_de_outra_versao(app):
    antiga = _linha_exemplo()
    nova = _versao(_zip(stops=PARADAS_LINHA + "E,Só na nova,-23.5,-46.64\n"))
    importar_linhas_paradas(nova.id)
    db.session.add(Trecho(
        versao_gtfs_id=antiga.id, linha_codigo="1012-10", sentido="0", trajeto="manual",
        ordem=1, parada_origem_codigo="D", parada_destino_codigo="E",
        geometria=[[-46.63, -23.5], [-46.64, -23.5]],
    ))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_linha_circular_nao_associa_a_primeira_parada_ao_fim_do_tracado():
    # Ida e volta pelo mesmo caminho; a primeira e a última parada coincidem.
    tracado = [(0, 0), (1, 0), (2, 0), (1, 0.0001), (0, 0.0001)]
    assert _indices_no_tracado([(0, 0.00009), (2, 0), (0, 0.00009)], tracado) == [0, 2, 4]


def test_zip_divergente_do_hash_nao_e_importado(app, tmp_path):
    versao = _versao()
    (tmp_path / "instance" / "gtfs" / f"{versao.hash}.zip").write_bytes(b"alterado")
    with pytest.raises(GTFSValidationError, match="divergente"):
        importar_linhas_paradas(versao.id)
    assert Linha.query.count() == 0


def test_geometria_no_mysql_e_linestring_nativo_convertido_de_e_para_wkt():
    from sqlalchemy import insert, select
    from sqlalchemy.dialects import mysql
    from sqlalchemy.schema import CreateTable

    dialeto = mysql.dialect()
    assert "geometria LINESTRING NOT NULL" in str(CreateTable(Trecho.__table__).compile(dialect=dialeto))
    assert "ST_AsText(trechos.geometria)" in str(select(Trecho.geometria).compile(dialect=dialeto))
    assert "ST_GeomFromText(%s)" in str(insert(Trecho).compile(dialect=dialeto))
    # ST_AsText precisa manter o tipo LineString para o WKT virar lista de pontos.
    leitura = select(Trecho.geometria).compile(dialect=dialeto)._result_columns[0][3]
    assert isinstance(leitura, LineString)
    assert leitura.process_result_value("LINESTRING(-46.6 -23.5,-46.61 -23.5)", dialeto) == [
        [-46.6, -23.5], [-46.61, -23.5]]
