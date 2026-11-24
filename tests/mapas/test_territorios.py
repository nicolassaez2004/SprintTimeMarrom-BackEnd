from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from pyproj import Transformer
from shapely.geometry import Polygon
from sqlalchemy import event
from uuid import uuid4

from app import create_app, db
from app.models import (
    ArquivoArmazenado,
    IndicadorOperacional,
    Linha,
    Parada,
    ParadaTerritorial,
    Trecho,
    TrechoTerritorial,
    UnidadeTerritorial,
    VersaoGTFS,
)
from app.territorios.associacao import associar_rede_a_territorios
from app.territorios.importacao import importar_limites_geojson, importar_snapshot_geosampa

UTC = timezone.utc
@pytest.fixture
def app(tmp_path):
    application = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite://",
        "MYSQL_SSL_CA": None,
    })
    application.instance_path = str(tmp_path / "instance")
    with application.app_context():
        event.listen(db.engine, "connect", lambda connection, _: connection.execute("PRAGMA foreign_keys=ON"))
        db.create_all()
        yield application
        db.session.remove()
        db.engine.dispose()


def _rede(versao_id="gtfs-test", paradas=(), geometria=None):
    agora = datetime(2026, 11, 24, tzinfo=UTC)
    arquivo = ArquivoArmazenado(
        id=f"arquivo-{versao_id}", chave=f"gtfs/{versao_id}.zip",
        nome_original="gtfs.zip", mime_type="application/zip", tamanho_bytes=0,
        sha256=(versao_id.encode().hex() + "0" * 64)[:64], criado_em=agora,
    )
    versao = VersaoGTFS(
        id=versao_id, hash=(versao_id.encode().hex() + "0" * 64)[:64],
        vigencia_inicio=date(2026, 1, 1), arquivo=arquivo,
    )
    linha = Linha(versao_gtfs=versao, codigo="8000-10", nome="Linha de teste")
    db.session.add_all([versao, linha])
    db.session.flush()
    for codigo, longitude, latitude in paradas:
        db.session.add(Parada(
            versao_gtfs_id=versao_id, codigo=codigo, nome=f"Parada {codigo}",
            longitude=longitude, latitude=latitude,
        ))
    if geometria:
        db.session.flush()
        db.session.add(Trecho(
            id=f"trecho-{versao_id}", versao_gtfs_id=versao_id,
            linha_codigo=linha.codigo, sentido="0", trajeto="trip-1", ordem=1,
            parada_origem_codigo=paradas[0][0], parada_destino_codigo=paradas[1][0],
            geometria=geometria,
        ))
    db.session.commit()
    return versao


def _geojson_bairros_sinteticos():
    para_metros = Transformer.from_crs(4326, 31983, always_xy=True)
    para_graus = Transformer.from_crs(31983, 4326, always_xy=True)
    x, y = para_metros.transform(-46.63, -23.55)
    limite = x

    def poligono(x0, x1):
        projetado = Polygon([
            (x0, y - 100), (x1, y - 100), (x1, y + 100),
            (x0, y + 100), (x0, y - 100),
        ])
        return {
            "type": "Polygon",
            "coordinates": [[list(point) for point in projetado.exterior.coords]],
        }

    distritos = {"type": "FeatureCollection", "crs": {"properties": {"name": "EPSG:31983"}},
                 "features": []}
    subprefeituras = {"type": "FeatureCollection", "crs": {"properties": {"name": "EPSG:31983"}},
                      "features": []}
    for index, (codigo, x0, x1) in enumerate((
        ("01", x - 100, limite), ("02", limite, x + 100),
    ), start=1):
        subpref_id = str(index)
        subprefeituras["features"].append({
            "type": "Feature", "properties": {
                "cd_identificador_subprefeitura": index,
                "cd_subprefeitura": codigo,
                "nm_subprefeitura": f"Subprefeitura {codigo}",
                "dt_atualizacao": "2026-11-16T00:00:00Z",
            }, "geometry": poligono(x0, x1),
        })
        distritos["features"].append({
            "type": "Feature", "properties": {
                "cd_identificador_distrito": 100 + index,
                "cd_identificador_subprefeitura": int(subpref_id),
                "cd_distrito_municipal": codigo,
                "nm_distrito_municipal": f"Distrito {codigo}",
                "dt_atualizacao": "2026-11-16T00:00:00Z",
            }, "geometry": poligono(x0, x1),
        })
    boundary_lon, boundary_lat = para_graus.transform(limite, y)
    line_coords = [
        para_graus.transform(x - 60, y),
        para_graus.transform(x + 40, y),
    ]
    return distritos, subprefeituras, (boundary_lon, boundary_lat), line_coords


def test_importa_snapshot_geosampa_com_versao_fonte_hash_e_estrutura(app):
    versao = importar_snapshot_geosampa()
    novamente = importar_snapshot_geosampa()
    assert novamente.id == versao.id
    assert versao.fonte.startswith("GeoSampa")
    assert versao.versao_fonte == "2026-11-16"
    assert versao.crs_origem == "EPSG:31983"
    assert len(versao.sha256) == 64
    assert versao.metadados["camadas"]["distritos"]["features"] == 96
    assert versao.metadados["camadas"]["subprefeituras"]["features"] == 32
    assert UnidadeTerritorial.query.filter_by(versao_id=versao.id, tipo="DISTRITO").count() == 96
    assert UnidadeTerritorial.query.filter_by(versao_id=versao.id, tipo="SUBPREFEITURA").count() == 32
    se = UnidadeTerritorial.query.filter_by(
        versao_id=versao.id, tipo="DISTRITO", codigo="78",
    ).one()
    assert se.nome == "SE" and se.subprefeitura.nome == "SE"


def test_amostra_oficial_praça_da_se_e_associada_a_se(app):
    territorial = importar_snapshot_geosampa()
    rede = _rede(
        paradas=[("P1", -46.6333, -23.5505), ("P2", -46.6340, -23.5510)],
        geometria=[[-46.6333, -23.5505], [-46.6340, -23.5510]],
    )
    resultado = associar_rede_a_territorios(rede.id, territorial.id)
    associacao = ParadaTerritorial.query.filter_by(
        versao_gtfs_id=rede.id, parada_codigo="P1", versao_territorial_id=territorial.id,
    ).one()
    assert (resultado.paradas_associadas, resultado.trechos_associados) == (2, 1)
    assert associacao.ponto_na_fronteira is False
    assert db.session.get(UnidadeTerritorial, associacao.distrito_id).nome == "SE"
    assert db.session.get(UnidadeTerritorial, associacao.subprefeitura_id).nome == "SE"
    assert TrechoTerritorial.query.count() == 1


def test_fronteira_de_parada_tem_desempate_estavel_e_trecho_nao_duplica(app):
    distritos, subprefeituras, (lon, lat), linha = _geojson_bairros_sinteticos()
    territorial = importar_limites_geojson(distritos, subprefeituras)
    rede = _rede(
        paradas=[("P1", lon, lat), ("P2", linha[1][0], linha[1][1])],
        geometria=[[linha[0][0], linha[0][1]], [linha[1][0], linha[1][1]]],
    )
    associacao = associar_rede_a_territorios(rede.id, territorial.id)
    parada = ParadaTerritorial.query.filter_by(
        versao_gtfs_id=rede.id, parada_codigo="P1",
    ).one()
    trecho = TrechoTerritorial.query.one()
    distrito = db.session.get(UnidadeTerritorial, trecho.distrito_id)
    subprefeitura = db.session.get(UnidadeTerritorial, trecho.subprefeitura_id)

    assert parada.ponto_na_fronteira is True
    assert db.session.get(UnidadeTerritorial, parada.distrito_id).codigo == "01"
    assert db.session.get(UnidadeTerritorial, parada.subprefeitura_id).codigo == "01"
    assert (distrito.codigo, subprefeitura.codigo) == ("01", "01")
    assert trecho.cruza_limite is True
    assert trecho.fracao_distrito == pytest.approx(0.6, abs=0.01)
    assert trecho.fracao_subprefeitura == pytest.approx(0.6, abs=0.01)
    assert associacao.paradas_na_fronteira == 1
    assert associacao.trechos_que_cruzam_limite == 1
    assert TrechoTerritorial.query.count() == 1
    assert trecho.qualidade["regra"] == "MAIOR_SOBREPOSICAO_COM_DESEMPATE_POR_CODIGO"


def test_recusa_dados_com_crs_ou_geometria_invalida(app):
    distritos, subprefeituras, _, _ = _geojson_bairros_sinteticos()
    distritos["crs"]["properties"]["name"] = "EPSG:4326"
    with pytest.raises(ValueError, match="CRS territorial inesperado"):
        importar_limites_geojson(distritos, subprefeituras)


def test_mapa_retorna_geojson_e_agrega_por_contagens_sem_media_de_percentuais(app):
    distritos, subprefeituras, _, linha_geom = _geojson_bairros_sinteticos()
    territorial = importar_limites_geojson(distritos, subprefeituras)
    gtfs_id = str(uuid4())
    rede = _rede(
        versao_id=gtfs_id,
        paradas=[("P1", -46.6301, -23.55), ("P2", -46.6302, -23.55)],
        geometria=[[linha_geom[0][0], linha_geom[0][1]], [linha_geom[1][0], linha_geom[1][1]]],
    )
    associar_rede_a_territorios(rede.id, territorial.id)
    inicio = datetime(2026, 11, 1, tzinfo=UTC)
    fim = datetime(2026, 11, 2, tzinfo=UTC)
    for parada, numerador, denominador in (("P1", 2, 5), ("P2", 3, 10)):
        db.session.add(IndicadorOperacional(
            versao_gtfs_id=gtfs_id, linha_codigo="8000-10", sentido="0",
            parada_codigo=parada, indicador="PONTUALIDADE_VIAGENS",
            valor=numerador / denominador * 100, unidade="%",
            periodo_inicio=inicio, periodo_fim=fim,
            contagem_numerador=numerador, contagem_denominador=denominador,
            versao_conjunto="conjunto-1", versao_correspondencia="correspondencia-1",
            versao_calculo="calculo-1", qualidade={"estado": "OK"},
        ))
    db.session.add(IndicadorOperacional(
        versao_gtfs_id=gtfs_id, linha_codigo="8000-10", sentido="0",
        parada_codigo="P1", indicador="TIPO_ZERO", valor=None, unidade="%",
        periodo_inicio=inicio, periodo_fim=fim,
        contagem_numerador=0, contagem_denominador=0,
        versao_conjunto="conjunto-1", versao_calculo="calculo-1",
    ))
    db.session.commit()

    cliente = app.test_client()
    base = f"versao_gtfs_id={gtfs_id}&inicio=2026-11-01T00:00:00Z&fim=2026-11-02T00:00:00Z"
    parada = cliente.get(f"/mapas/indicadores?escopo=parada&{base}")
    assert parada.status_code == 200
    assert parada.json["features"][0]["geometry"]["type"] == "Point"
    assert parada.json["features"][0]["properties"]["distrito_id"]

    distrito = cliente.get(
        f"/mapas/indicadores?escopo=distrito&base=parada&tipo=PONTUALIDADE_VIAGENS&{base}"
    )
    assert distrito.status_code == 200
    feature = distrito.json["features"][0]
    assert feature["geometry"]["type"] == "Polygon"
    assert feature["properties"]["contagens"] == {"numerador": 5, "denominador": 15}
    assert feature["properties"]["valor"] == pytest.approx(100 * 5 / 15)
    assert feature["properties"]["valor"] != pytest.approx((40 + 30) / 2)
    assert any(
        item["properties"]["ausencia"] == "SEM_DADOS"
        for item in distrito.json["features"]
    )

    zero = cliente.get(f"/mapas/indicadores?escopo=distrito&base=parada&tipo=TIPO_ZERO&{base}")
    assert zero.status_code == 200
    assert zero.json["features"][0]["properties"]["valor"] is None
    assert zero.json["features"][0]["properties"]["ausencia"] == "DENOMINADOR_ZERO"


def test_mapa_valida_base_de_agregacao_e_consulta_trecho(app):
    distritos, subprefeituras, _, linha_geom = _geojson_bairros_sinteticos()
    territorial = importar_limites_geojson(distritos, subprefeituras)
    gtfs_id = str(uuid4())
    rede = _rede(
        versao_id=gtfs_id,
        paradas=[("P1", -46.6301, -23.55), ("P2", -46.6302, -23.55)],
        geometria=[[linha_geom[0][0], linha_geom[0][1]], [linha_geom[1][0], linha_geom[1][1]]],
    )
    associar_rede_a_territorios(rede.id, territorial.id)
    trecho = Trecho.query.one()
    db.session.add(IndicadorOperacional(
        versao_gtfs_id=gtfs_id, linha_codigo="8000-10", sentido="0",
        trecho_id=trecho.id, indicador="CUMPRIMENTO_VIAGENS", valor=100,
        unidade="%", periodo_inicio=datetime(2026, 11, 1, tzinfo=UTC),
        periodo_fim=datetime(2026, 11, 2, tzinfo=UTC),
        contagem_numerador=1, contagem_denominador=1, versao_conjunto="v1",
    ))
    db.session.commit()

    cliente = app.test_client()
    assert cliente.get("/mapas/indicadores?escopo=distrito").status_code == 400
    resposta = cliente.get(
        f"/mapas/indicadores?escopo=trecho&versao_gtfs_id={gtfs_id}"
    )
    assert resposta.status_code == 200
    feature = resposta.json["features"][0]
    assert feature["geometry"]["type"] == "LineString"
    assert feature["properties"]["cruza_limite"] is True
    assert feature["properties"]["fracao_distrito"] == pytest.approx(0.6, abs=0.01)


def test_mapa_nao_converte_ausencia_em_zero_nem_agrega_unidade_nao_percentual(app):
    distritos, subprefeituras, _, linha_geom = _geojson_bairros_sinteticos()
    territorial = importar_limites_geojson(distritos, subprefeituras)
    gtfs_id = str(uuid4())
    rede = _rede(
        versao_id=gtfs_id,
        paradas=[("P1", -46.6301, -23.55), ("P2", -46.6302, -23.55)],
        geometria=[[linha_geom[0][0], linha_geom[0][1]], [linha_geom[1][0], linha_geom[1][1]]],
    )
    associar_rede_a_territorios(rede.id, territorial.id)
    inicio = datetime(2026, 11, 1, tzinfo=UTC)
    fim = datetime(2026, 11, 1, 1, tzinfo=UTC)
    comum = dict(versao_gtfs_id=gtfs_id, linha_codigo="8000-10", sentido="0",
                 periodo_inicio=inicio, periodo_fim=fim, versao_conjunto="v1")
    db.session.add(IndicadorOperacional(
        parada_codigo="P1", indicador="X_INSUF", valor=None, unidade="%",
        contagem_numerador=0, contagem_denominador=4,
        qualidade={"dados_insuficientes": True}, **comum,
    ))
    for parada, passagens in (("P1", 6), ("P2", 4)):
        db.session.add(IndicadorOperacional(
            parada_codigo=parada, indicador="FREQUENCIA_OBSERVADA", valor=passagens,
            unidade="passagens/h", contagem_numerador=passagens,
            contagem_denominador=None, **comum,
        ))
    db.session.commit()

    cliente = app.test_client()
    base = f"versao_gtfs_id={gtfs_id}&escopo=distrito&base=parada"
    insuficiente = cliente.get(f"/mapas/indicadores?{base}&tipo=X_INSUF").json
    propriedades = insuficiente["features"][0]["properties"]
    assert propriedades["valor"] is None
    assert propriedades["ausencia"] == "DADOS_INSUFICIENTES"

    frequencia = cliente.get(f"/mapas/indicadores?{base}&tipo=FREQUENCIA_OBSERVADA").json
    propriedades = frequencia["features"][0]["properties"]
    assert propriedades["valor"] is None
    assert propriedades["ausencia"] == "AGREGACAO_NAO_SUPORTADA"
    assert set(frequencia["legenda"]["ausencia"]) >= {
        "AGREGACAO_NAO_SUPORTADA", "VALOR_INDISPONIVEL", "DADOS_INSUFICIENTES",
    }

    inexistente = cliente.get(f"/mapas/indicadores?{base}&versao_territorial_id={uuid4()}")
    assert inexistente.status_code == 400
    assert inexistente.json["erro"]["campo"] == "versao_territorial_id"
