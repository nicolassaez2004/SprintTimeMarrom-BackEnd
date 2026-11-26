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
# Envelope generoso da Grande São Paulo (lon_min, lat_min, lon_max, lat_max). Não é
# o limite municipal: só detecta CRS declarado errado ou latitude/longitude trocadas.
ENVELOPE_SAO_PAULO = (-47.5, -24.5, -45.5, -23.0)


def _coordenadas(geom):
    partes = geom.geoms if hasattr(geom, "geoms") else [geom]
    return [ponto for parte in partes for ponto in parte.coords]


def _dentro_do_envelope(geom):
    lon_min, lat_min, lon_max, lat_max = ENVELOPE_SAO_PAULO
    return all(lon_min <= lon <= lon_max and lat_min <= lat <= lat_max
               for lon, lat, *_ in _coordenadas(geom))


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
                     categoria, capturada_em=None, id_field=None, filtro=None):
    """Persiste um snapshot GeoJSON; repetir o mesmo arquivo/fonte/filtro é idempotente.

    ``filtro``: ``{atributo: [valores aceitos]}`` (ex.: só corredores "Em Operação").
    Feições fora do filtro não viram infraestrutura, mas ficam listadas na versão;
    o arquivo original não é alterado. Os valores aceitos são decisão do PO.
    """
    if filtro is not None and (not isinstance(filtro, dict) or not filtro or not all(
            isinstance(v, list) and v for v in filtro.values())):
        raise ValueError("filtro deve ser {atributo: [valores aceitos]}")
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
    crs_arquivo = ((collection.get("crs") or {}).get("properties") or {}).get("name")
    if crs_arquivo and not crs_obj.equals(CRS.from_user_input(crs_arquivo), ignore_axis_order=True):
        raise ValueError(f"crs_origem {crs_origem} diverge do CRS declarado no arquivo ({crs_arquivo})")
    transformer = Transformer.from_crs(crs_obj, CRS_DESTINO, always_xy=True)
    feicoes, vistos, excluidas, por_geometria, duplicadas = [], set(), {}, {}, []
    for i, feature in enumerate(collection["features"]):
        props = (feature or {}).get("properties") or {}
        fid = props.get(id_field, feature.get("id")) if id_field else feature.get("id")
        fid = str(fid if fid is not None else i)
        if fid in vistos:
            raise ValueError(f"identificador de feição repetido no snapshot: {fid}")
        vistos.add(fid)
        try:
            geom = shape(feature["geometry"])
        except (KeyError, TypeError, AttributeError, ValueError, IndexError):
            raise ValueError(f"feição {fid} sem geometria GeoJSON legível") from None
        if geom.is_empty or not geom.is_valid or geom.geom_type not in {"LineString", "MultiLineString"}:
            raise ValueError(f"feição {fid} deve ter geometria linear válida e não vazia")
        geom = transform(geom, transformer.transform, interleaved=False)
        if geom.is_empty or not geom.is_valid or geom.length <= 0:
            raise ValueError(f"feição {fid} inválida ou sem comprimento após conversão para WGS84")
        if not _dentro_do_envelope(geom):
            raise ValueError(f"feição {fid} fora da região de São Paulo após conversão: "
                             "confira crs_origem e a ordem longitude/latitude")
        fora = [campo for campo, aceitos in (filtro or {}).items() if props.get(campo) not in aceitos]
        if fora:
            excluidas[fid] = {campo: props.get(campo) for campo in fora}
            continue
        # Mesma geometria com outro identificador: mantida (a classificação une), mas registrada.
        assinatura = geom.normalize().wkb_hex
        if assinatura in por_geometria:
            duplicadas.append([fid, por_geometria[assinatura]])
        por_geometria.setdefault(assinatura, fid)
        feicoes.append((fid, mapping(geom), props))
    if not feicoes:
        raise ValueError("nenhuma feição restou após o filtro")
    checksum_arquivo = hashlib.sha256(conteudo).hexdigest()
    # Identidade da importação: o arquivo e, se houver, o filtro aplicado a ele.
    checksum = checksum_arquivo if filtro is None else hashlib.sha256(
        (checksum_arquivo + json.dumps(filtro, sort_keys=True, ensure_ascii=False)).encode()).hexdigest()
    existente = VersaoInfraestrutura.query.filter_by(fonte=fonte, sha256=checksum).one_or_none()
    if existente:
        if existente.metadados.get("categoria") != categoria:
            raise ValueError(f"o mesmo arquivo já foi importado como {existente.metadados.get('categoria')}")
        return existente
    capturada_em = capturada_em or datetime.now(timezone.utc)
    # Mudanças só se comparam com a versão anterior da mesma fonte e camada:
    # corredores e faixas são arquivos distintos.
    anterior = next((v for v in VersaoInfraestrutura.query.filter(
        VersaoInfraestrutura.fonte == fonte, VersaoInfraestrutura.capturada_em <= capturada_em,
    ).order_by(VersaoInfraestrutura.capturada_em.desc(), VersaoInfraestrutura.id.desc())
        if v.metadados.get("categoria") == categoria), None)
    versao_id = str(uuid5(NAMESPACE_URL, f"infraestrutura:{fonte}:{checksum}"))
    versao = VersaoInfraestrutura(
        id=versao_id, fonte=fonte, url_fonte=url_fonte, versao_fonte=str(versao_fonte),
        sha256=checksum, crs_origem=crs_obj.to_string(), capturada_em=capturada_em,
        metadados={"crs_armazenado": CRS_DESTINO, "categoria": categoria,
                   "feicoes": len(feicoes), "sha256_arquivo": checksum_arquivo,
                   # Para reproduzir: arquivo original (imutável), identificador e filtro.
                   "arquivo_origem": None if isinstance(origem, dict) else Path(origem).name,
                   "id_field": id_field, "filtro": filtro, "excluidas_por_filtro": excluidas,
                   "geometrias_duplicadas": duplicadas,
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
