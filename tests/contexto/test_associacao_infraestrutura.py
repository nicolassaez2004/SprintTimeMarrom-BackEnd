from datetime import datetime, timezone
import math

from app import create_app, db
from app.contexto.infraestrutura.associacao import classificar_trechos
from app.contexto.infraestrutura.importacao import importar_geojson
from app.models import AssociacaoInfraestruturaTrecho, Trecho


def _feature(fid, inicio, fim):
    return {"type": "Feature", "id": fid, "properties": {},
            "geometry": {"type": "LineString", "coordinates": [inicio, fim]}}


def test_classifica_misto_parcial_e_sem_cobertura_sem_inferir_via_comum(tmp_path):
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://", "MYSQL_SSL_CA": None})
    app.instance_path = str(tmp_path / "instance")
    with app.app_context():
        db.create_all()
        rede_id = "gtfs-infra-test"
        linha = [[-46.6, -23.5], [-46.59, -23.5]]
        Trechos = [
            Trecho(id="trecho-misto", versao_gtfs_id=rede_id, linha_codigo="L1", sentido="0",
                   trajeto="T1", ordem=1, parada_origem_codigo="P1", parada_destino_codigo="P2",
                   geometria=linha),
            Trecho(id="trecho-sem-cobertura", versao_gtfs_id=rede_id, linha_codigo="L1", sentido="0",
                   trajeto="T1", ordem=2, parada_origem_codigo="P2", parada_destino_codigo="P3",
                   geometria=[[-46.4, -23.5], [-46.39, -23.5]]),
            Trecho(id="trecho-parcial", versao_gtfs_id=rede_id, linha_codigo="L1", sentido="0",
                   trajeto="T1", ordem=3, parada_origem_codigo="P3", parada_destino_codigo="P4",
                   geometria=[[-46.6, -23.51], [-46.59, -23.51]]),
        ]
        db.session.add_all(Trechos)
        db.session.commit()
        momento = datetime(2026, 11, 26, tzinfo=timezone.utc)
        url = "https://example.test/geosampa"
        corredor = importar_geojson(
            {"type": "FeatureCollection", "features": [
                _feature("corredor-1", linha[0], [-46.595, -23.5]),
                _feature("corredor-2", [-46.6, -23.51], [-46.595, -23.51]),
            ]}, fonte="GeoSampa", url_fonte=url, versao_fonte="2026-11",
            crs_origem="EPSG:4326", categoria="CORREDOR", capturada_em=momento,
        )
        faixa = importar_geojson(
            {"type": "FeatureCollection", "features": [
                _feature("faixa-1", [-46.595, -23.5], linha[1]),
            ]}, fonte="GeoSampa", url_fonte=url, versao_fonte="2026-11",
            crs_origem="EPSG:4326", categoria="FAIXA_EXCLUSIVA", capturada_em=momento,
        )
        osm_conflitante = importar_geojson(
            {"type": "FeatureCollection", "features": [
                _feature("way-conflict", linha[0], [-46.595, -23.5]),
            ]}, fonte="OpenStreetMap", url_fonte="https://overpass.example.test",
            versao_fonte="2026-11", crs_origem="EPSG:4326",
            categoria="FAIXA_EXCLUSIVA", capturada_em=momento,
        )
        resumo = classificar_trechos(rede_id, versoes=[corredor, faixa, osm_conflitante])
        assert resumo["trechos"] == 3
        assert resumo["classificacoes"]["MISTO"] == 1
        assert resumo["classificacoes"]["COBERTURA_PARCIAL"] == 1
        assert resumo["classificacoes"]["SEM_COBERTURA"] == 1
        assert resumo["sem_cobertura"] == 2
        misto = AssociacaoInfraestruturaTrecho.query.filter_by(trecho_id="trecho-misto").one()
        assert set(misto.categorias_confirmadas) == {"CORREDOR", "FAIXA_EXCLUSIVA"}
        assert math.isclose(misto.fracao_corredor, 0.5, abs_tol=0.03)
        assert math.isclose(misto.fracao_faixa, 0.5, abs_tol=0.03)
        assert all(item["fonte"] == "GeoSampa" for item in misto.feicoes_associadas)
        sem = AssociacaoInfraestruturaTrecho.query.filter_by(trecho_id="trecho-sem-cobertura").one()
        assert sem.classificacao == "SEM_COBERTURA"
        assert sem.regra["via_comum_confirmada"] is False
        assert sem.fracao_sem_cobertura == 1
        resposta = app.test_client().get(
            "/trechos/trecho-misto/infraestrutura?data=2026-11-26",
        )
        assert resposta.status_code == 200
        assert resposta.json["classificacao"] == "MISTO"
        assert {fonte["versao_fonte"] for fonte in resposta.json["fontes"]} == {"2026-11"}
        db.session.remove()
        db.drop_all()
