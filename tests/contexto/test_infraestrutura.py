from app import create_app, db
from datetime import date, datetime, timezone

from app.contexto.infraestrutura.agenda import atualizar_mensal
from app.contexto.infraestrutura.consulta import consultar_em
from app.contexto.infraestrutura.importacao import importar_geojson
from app.contexto.infraestrutura.tarefa import atualizar_infraestrutura
from app.models import AtualizacaoMensalInfraestrutura, ClassificacaoInfraestrutura


def test_importa_normaliza_registra_origem_e_reutiliza_snapshot(tmp_path):
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://", "MYSQL_SSL_CA": None})
    app.instance_path = str(tmp_path / "instance")
    with app.app_context():
        db.create_all()
        dados = {"type": "FeatureCollection", "features": [{
            "type": "Feature", "id": "via-1", "properties": {"nome": "Av. Teste"},
            "geometry": {"type": "LineString", "coordinates": [[-46.6, -23.5], [-46.5, -23.4]]},
        }]}
        primeira = importar_geojson(
            dados, fonte="OpenStreetMap", url_fonte="https://overpass-api.de/api/interpreter",
            versao_fonte="snapshot teste", crs_origem="EPSG:4326", categoria="FAIXA_EXCLUSIVA",
        )
        segunda = importar_geojson(
            dados, fonte="OpenStreetMap", url_fonte="https://overpass-api.de/api/interpreter",
            versao_fonte="snapshot teste", crs_origem="EPSG:4326", categoria="FAIXA_EXCLUSIVA",
        )
        feicao = ClassificacaoInfraestrutura.query.one()
        assert primeira.id == segunda.id
        assert primeira.metadados["crs_armazenado"] == "EPSG:4326"
        assert feicao.categoria == "FAIXA_EXCLUSIVA"
        assert feicao.geometria["coordinates"][0] == [-46.6, -23.5]
        db.session.remove()
        db.drop_all()


def test_historico_e_relatorio_de_mudancas_preservam_a_versao_antiga(tmp_path):
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://", "MYSQL_SSL_CA": None})
    app.instance_path = str(tmp_path / "instance")
    with app.app_context():
        db.create_all()
        origem = "https://overpass-api.de/api/interpreter"
        base = {"type": "FeatureCollection", "features": [
            {"type": "Feature", "id": "way-7", "properties": {"name": "Rua A"},
             "geometry": {"type": "LineString", "coordinates": [[-46.6, -23.5], [-46.5, -23.4]]}},
            {"type": "Feature", "id": "way-8", "properties": {"name": "Rua B"},
             "geometry": {"type": "LineString", "coordinates": [[-46.4, -23.5], [-46.3, -23.4]]}},
        ]}
        primeira_data = datetime(2026, 11, 2, tzinfo=timezone.utc)
        primeira = importar_geojson(
            base, fonte="OpenStreetMap", url_fonte=origem, versao_fonte="2026-11-02",
            crs_origem="EPSG:4326", categoria="FAIXA_EXCLUSIVA", capturada_em=primeira_data,
        )
        alterada = {**base, "features": [
            {**base["features"][0], "properties": {"name": "Rua A alterada"}},
            {**base["features"][1], "geometry": {"type": "LineString",
              "coordinates": [[-46.4, -23.5], [-46.25, -23.4]]}},
        ]}
        resultado = atualizar_mensal(primeira_data.replace(day=26), lambda _: [{
            "origem": alterada, "fonte": "OpenStreetMap", "url_fonte": origem,
            "versao_fonte": "2026-11-26", "crs_origem": "EPSG:4326",
            "categoria": "FAIXA_EXCLUSIVA", "capturada_em": datetime(2026, 11, 26, tzinfo=timezone.utc),
        }])
        consulta_antiga = consultar_em("OpenStreetMap", "FAIXA_EXCLUSIVA", date(2026, 11, 10))
        consulta_nova = consultar_em("OpenStreetMap", "FAIXA_EXCLUSIVA", date(2026, 11, 27))
        nova = consulta_nova["versao"]
        assert consulta_antiga["versao"].id == primeira.id
        assert consulta_antiga["classificacoes"][0].atributos["name"] == "Rua A"
        assert nova.metadados["versao_anterior_id"] == primeira.id
        assert [(m["identificador_fonte"], m["tipo"]) for m in nova.metadados["mudancas"]] == [
            ("way-7", "GEOMETRIA_OU_ATRIBUTOS_ALTERADOS"), ("way-8", "GEOMETRIA_OU_ATRIBUTOS_ALTERADOS")]
        assert resultado["versoes"][0]["versao_id"] == nova.id
        chamadas = []
        repetido = atualizar_mensal(primeira_data.replace(day=27), lambda _: chamadas.append(True))
        assert repetido == resultado
        assert chamadas == []
        assert AtualizacaoMensalInfraestrutura.query.one().estado == "OK"
        db.session.remove()
        db.drop_all()


def test_worker_importa_manifesto_mensal_e_registra_resultado(tmp_path):
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://", "MYSQL_SSL_CA": None})
    app.instance_path = str(tmp_path / "instance")
    with app.app_context():
        db.create_all()
        snapshot = tmp_path / "entrada.geojson"
        snapshot.write_text('{"type":"FeatureCollection","features":[{"type":"Feature","id":"x",'
                            '"properties":{},"geometry":{"type":"LineString","coordinates":'
                            '[[-46.6,-23.5],[-46.5,-23.4]]}}]}', encoding="utf-8")
        manifesto = tmp_path / "manifest.json"
        manifesto.write_text('{"bases":[{"arquivo":"entrada.geojson","fonte":"GeoSampa",'
                             '"url_fonte":"https://example.test/wfs","versao_fonte":"snapshot-1",'
                             '"crs_origem":"EPSG:4326","categoria":"CORREDOR"}]}', encoding="utf-8")
        app.config["INFRAESTRUTURA_SNAPSHOT_MANIFEST"] = manifesto
        with app.test_request_context():
            from flask import current_app
            current_app.config.update(app.config)
            resultado = atualizar_infraestrutura(datetime(2026, 11, 26, tzinfo=timezone.utc))
        assert len(resultado["versoes"]) == 1
        assert AtualizacaoMensalInfraestrutura.query.one().estado == "OK"
        db.session.remove()
        db.drop_all()
