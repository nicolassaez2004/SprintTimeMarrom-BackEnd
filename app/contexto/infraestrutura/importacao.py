"""Importa snapshots GeoJSON de infraestrutura em WGS84.

GeoSampa fornece corredores e faixas exclusivas como camadas distintas. No
OSM são aceitas vias com lanes:bus, bus:lanes ou busway; o mapeamento é
explicitado por ``categoria_por_feature`` no arquivo de entrada. Divergências
entre fontes não são mescladas: cada fonte permanece consultável e versionada,
e a camada oficial GeoSampa é a referência para a categoria; OSM complementa
cobertura e deve ser tratado como indício quando conflitar.
"""
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from pyproj import CRS, Transformer
from shapely import transform
from shapely.geometry import mapping, shape

from app import db
from app.models import ClassificacaoInfraestrutura, VersaoInfraestrutura

CRS_DESTINO = "EPSG:4326"
CATEGORIAS = {"CORREDOR", "FAIXA_EXCLUSIVA"}


def _chave_feicao(geometria, atributos, categoria):
    return json.dumps({"geometria": geometria, "atributos": atributos,
                       "categoria": categoria}, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))


def comparar_versoes(versao_anterior, versao_nova):
    """Lista alterações por identificador estável, sem alterar snapshots antigos."""
    antigas = {f.identificador_fonte: f for f in ClassificacaoInfraestrutura.query.filter_by(
        versao_id=versao_anterior.id).all()} if versao_anterior else {}
    novas = {f.identificador_fonte: f for f in ClassificacaoInfraestrutura.query.filter_by(
        versao_id=versao_nova.id).all()}
    mudancas = []
    for identificador in sorted(antigas.keys() | novas.keys()):
        antes, depois = antigas.get(identificador), novas.get(identificador)
        if antes is None:
            mudancas.append({"identificador_fonte": identificador, "tipo": "ADICIONADA",
                             "antes": None, "depois": {"categoria": depois.categoria,
                             "geometria": depois.geometria, "atributos": depois.atributos}})
            continue
        elif depois is None:
            mudancas.append({"identificador_fonte": identificador, "tipo": "REMOVIDA",
                             "antes": {"categoria": antes.categoria, "geometria": antes.geometria,
                                       "atributos": antes.atributos}, "depois": None})
            continue
        antes_valor = {"categoria": antes.categoria, "geometria": antes.geometria,
                       "atributos": antes.atributos}
        depois_valor = {"categoria": depois.categoria, "geometria": depois.geometria,
                        "atributos": depois.atributos}
        if antes.categoria != depois.categoria:
            mudancas.append({"identificador_fonte": identificador, "tipo": "CATEGORIA_ALTERADA",
                             "antes": antes_valor, "depois": depois_valor})
        if _chave_feicao(antes.geometria, antes.atributos, "") != _chave_feicao(
                depois.geometria, depois.atributos, ""):
            mudancas.append({"identificador_fonte": identificador,
                             "tipo": "GEOMETRIA_OU_ATRIBUTOS_ALTERADOS",
                             "antes": antes_valor, "depois": depois_valor})
    return mudancas


def importar_geojson(origem, *, fonte, url_fonte, versao_fonte, crs_origem,
                     categoria, capturada_em=None, id_field=None):
    """Persiste um snapshot GeoJSON; repetir o mesmo arquivo/fonte é idempotente."""
    if fonte not in {"GeoSampa", "OpenStreetMap"}:
        raise ValueError("fonte deve ser GeoSampa ou OpenStreetMap")
    if categoria not in CATEGORIAS:
        raise ValueError("categoria deve ser CORREDOR ou FAIXA_EXCLUSIVA")
    if isinstance(origem, dict):
        conteudo = json.dumps(origem, sort_keys=True, separators=(",", ":")).encode()
        collection = origem
    else:
        conteudo = Path(origem).read_bytes()
        if str(origem).endswith(".gz"):
            conteudo = gzip.decompress(conteudo)
        collection = json.loads(conteudo)
    if collection.get("type") != "FeatureCollection" or not collection.get("features"):
        raise ValueError("infraestrutura precisa ser uma FeatureCollection GeoJSON não vazia")
    crs_obj = CRS.from_user_input(crs_origem)
    transformer = Transformer.from_crs(crs_obj, CRS_DESTINO, always_xy=True)
    feicoes = []
    for i, feature in enumerate(collection["features"]):
        geom = shape(feature.get("geometry"))
        if geom.is_empty or not geom.is_valid or geom.geom_type not in {"LineString", "MultiLineString"}:
            raise ValueError("cada feição deve ter geometria linear válida e não vazia")
        geom = transform(geom, transformer.transform, interleaved=False)
        if geom.is_empty or not geom.is_valid:
            raise ValueError("geometria inválida após conversão para WGS84")
        props = feature.get("properties") or {}
        fid = props.get(id_field, feature.get("id")) if id_field else feature.get("id")
        fid = str(fid if fid is not None else i)
        feicoes.append((fid, mapping(geom), props))
    checksum = hashlib.sha256(conteudo).hexdigest()
    existente = next((v for v in VersaoInfraestrutura.query.filter_by(
        fonte=fonte, sha256=checksum).all()
        if v.metadados.get("categoria") == categoria), None)
    if existente:
        return existente
    anterior = (VersaoInfraestrutura.query.filter_by(fonte=fonte)
                .order_by(VersaoInfraestrutura.capturada_em.desc(), VersaoInfraestrutura.id.desc())
                .first())
    versao_id = str(uuid5(NAMESPACE_URL, f"infraestrutura:{fonte}:{checksum}"))
    versao = VersaoInfraestrutura(
        id=versao_id, fonte=fonte, url_fonte=url_fonte, versao_fonte=str(versao_fonte),
        sha256=checksum, crs_origem=crs_obj.to_string(),
        capturada_em=capturada_em or datetime.now(timezone.utc),
        metadados={"crs_armazenado": CRS_DESTINO, "categoria": categoria,
                   "feicoes": len(feicoes), "sha256_arquivo": checksum,
                   "regra_divergencia": "GeoSampa é referência de categoria; OSM complementa e conflitos ficam separados."},
    )
    db.session.add(versao)
    db.session.flush()
    for fid, geometria, atributos in feicoes:
        db.session.add(ClassificacaoInfraestrutura(
            id=str(uuid5(NAMESPACE_URL, f"{versao_id}:{fid}")), versao_id=versao_id,
            identificador_fonte=fid, categoria=categoria,
            geometria=geometria, atributos=atributos,
        ))
    db.session.flush()
    mudancas = comparar_versoes(anterior, versao)
    versao.metadados = {**versao.metadados,
                        "versao_anterior_id": anterior.id if anterior else None,
                        "mudancas": mudancas,
                        "resumo_mudancas": {tipo: sum(m["tipo"] == tipo for m in mudancas)
                                            for tipo in sorted({m["tipo"] for m in mudancas})}}
    db.session.commit()
    return versao
