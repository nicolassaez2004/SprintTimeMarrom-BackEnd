"""Import official GeoSampa district/subprefecture GeoJSON as a snapshot."""

from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from pyproj import Transformer
from shapely import transform
from shapely.geometry import mapping, shape

from app import db
from app.models import UnidadeTerritorial, VersaoTerritorial

FONTE = "GeoSampa — Prefeitura de São Paulo — SMUL/GEOINFO"
URL_WFS = "https://wfs.geosampa.prefeitura.sp.gov.br/geoserver/geoportal/wfs"
CRS_ORIGEM = "EPSG:31983"
CRS_DESTINO = "EPSG:4326"
VERSAO_REGRA = "territorios-1.0"
_DADOS = Path(__file__).resolve().parents[2] / "data" / "territorios" / "geosampa_2026-11-16"


def _abrir_geojson(origem):
    if isinstance(origem, dict):
        bruto = json.dumps(origem, sort_keys=True, separators=(",", ":")).encode()
        return origem, bruto
    caminho = Path(origem)
    conteudo = caminho.read_bytes()
    if caminho.suffix == ".gz":
        conteudo = gzip.decompress(conteudo)
    return json.loads(conteudo), conteudo


def _crs(collection):
    nome = collection.get("crs", {}).get("properties", {}).get("name", CRS_ORIGEM)
    if "31983" not in nome:
        raise ValueError(f"CRS territorial inesperado: {nome}")
    return CRS_ORIGEM


def _features(collection, fields, transformer):
    if collection.get("type") != "FeatureCollection":
        raise ValueError("limites devem estar em uma FeatureCollection GeoJSON")
    features = collection.get("features")
    if not isinstance(features, list) or not features:
        raise ValueError("camada territorial não contém feições")

    saida = []
    for feature in features:
        propriedades = feature.get("properties") or {}
        ausentes = [campo for campo in fields if propriedades.get(campo) is None]
        if ausentes:
            raise ValueError(f"atributos territoriais ausentes: {', '.join(ausentes)}")
        geometria = shape(feature.get("geometry"))
        if geometria.geom_type not in ("Polygon", "MultiPolygon") or geometria.is_empty or not geometria.is_valid:
            raise ValueError("limite precisa ser Polygon/MultiPolygon válido e não vazio")
        wgs84 = transform(geometria, transformer.transform, interleaved=False)
        saida.append((propriedades, mapping(wgs84)))
    return saida


def importar_limites_geojson(distritos, subprefeituras, *, capturada_em=None):
    """Persist one immutable boundary snapshot from GeoSampa GeoJSON.

    Inputs use GeoSampa's EPSG:31983. Stored geometries are normalized to
    RFC 7946-style longitude/latitude so map clients can consume them directly.
    """
    distritos_json, bytes_distritos = _abrir_geojson(distritos)
    subpref_json, bytes_subpref = _abrir_geojson(subprefeituras)
    crs = _crs(distritos_json)
    if _crs(subpref_json) != crs:
        raise ValueError("distritos e subprefeituras precisam usar o mesmo CRS")
    para_wgs84 = Transformer.from_crs(crs, CRS_DESTINO, always_xy=True)

    subpref_features = _features(
        subpref_json, ("cd_identificador_subprefeitura", "cd_subprefeitura",
                       "nm_subprefeitura", "dt_atualizacao"), para_wgs84,
    )
    distrito_features = _features(
        distritos_json, ("cd_identificador_distrito", "cd_identificador_subprefeitura", "cd_distrito_municipal",
                         "nm_distrito_municipal", "dt_atualizacao"), para_wgs84,
    )
    subpref_por_identificador = {
        str(props["cd_identificador_subprefeitura"]): props
        for props, _ in subpref_features
    }
    if len(subpref_por_identificador) != len(subpref_features):
        raise ValueError("código de subprefeitura repetido na camada oficial")
    for props, _ in distrito_features:
        if str(props["cd_identificador_subprefeitura"]) not in subpref_por_identificador:
            raise ValueError("distrito referencia subprefeitura ausente na mesma versão")

    checksum = hashlib.sha256(bytes_distritos + b"\0" + bytes_subpref).hexdigest()
    existente = VersaoTerritorial.query.filter_by(sha256=checksum).one_or_none()
    if existente is not None:
        return existente

    datas = [props["dt_atualizacao"][:10] for props, _ in (*subpref_features, *distrito_features)]
    versao_fonte = max(datas)
    versao_id = str(uuid5(NAMESPACE_URL, f"geosampa:{checksum}"))
    instante = capturada_em or datetime.now(timezone.utc)
    versao = VersaoTerritorial(
        id=versao_id,
        fonte=FONTE,
        url_fonte=f"{URL_WFS}?service=WFS&version=2.0.0&request=GetFeature",
        versao_fonte=versao_fonte,
        sha256=checksum,
        crs_origem=crs,
        capturada_em=instante,
        metadados={
            "crs_armazenado": CRS_DESTINO,
            "versao_regra_associacao": VERSAO_REGRA,
            "camadas": {
                "distritos": {
                    "type_name": "geoportal:distrito_municipal",
                    "features": len(distrito_features),
                    "sha256": hashlib.sha256(bytes_distritos).hexdigest(),
                },
                "subprefeituras": {
                    "type_name": "geoportal:subprefeitura",
                    "features": len(subpref_features),
                    "sha256": hashlib.sha256(bytes_subpref).hexdigest(),
                },
            },
        },
    )
    db.session.add(versao)
    db.session.flush()

    unidades_subprefeitura = {}
    for props, geometria in subpref_features:
        identificador = str(props["cd_identificador_subprefeitura"])
        unidade = UnidadeTerritorial(
            id=str(uuid5(NAMESPACE_URL, f"{versao_id}:SUBPREFEITURA:{props['cd_subprefeitura']}")),
            versao_id=versao_id,
            tipo="SUBPREFEITURA",
            codigo=str(props["cd_subprefeitura"]),
            nome=props["nm_subprefeitura"],
            identificador_fonte=identificador,
            geometria=geometria,
        )
        unidades_subprefeitura[identificador] = unidade
        db.session.add(unidade)
    db.session.flush()

    for props, geometria in distrito_features:
        codigo = str(props["cd_distrito_municipal"])
        subpref_identificador = str(props["cd_identificador_subprefeitura"])
        db.session.add(UnidadeTerritorial(
            id=str(uuid5(NAMESPACE_URL, f"{versao_id}:DISTRITO:{codigo}")),
            versao_id=versao_id,
            tipo="DISTRITO",
            codigo=codigo,
            nome=props["nm_distrito_municipal"],
            identificador_fonte=str(props["cd_identificador_distrito"]),
            subprefeitura_id=unidades_subprefeitura[subpref_identificador].id,
            geometria=geometria,
        ))
    db.session.commit()
    return versao


def importar_snapshot_geosampa():
    """Import the checked-in, hashed 2026-11-16 GeoSampa boundary snapshot."""
    return importar_limites_geojson(
        _DADOS / "distritos.geojson.gz",
        _DADOS / "subprefeituras.geojson.gz",
        capturada_em=datetime(2026, 11, 24, tzinfo=timezone.utc),
    )
