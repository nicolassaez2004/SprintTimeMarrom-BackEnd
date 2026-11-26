"""Associa geometrias de trechos a snapshots de infraestrutura viária.

Regra v1: converte trechos e eixos da infraestrutura de WGS84 para SIRGAS
2000/UTM 23S, cria faixa de proximidade de 10 m em torno das feições e mede o
comprimento do trecho coberto por cada categoria. GeoSampa prevalece sobre
OSM nas porções em conflito; OSM só complementa onde GeoSampa não mapeia uma
feição positiva. Não haver corredor/faixa na base não prova que a via é comum:
sem base completa de vias, o resultado permanece SEM_COBERTURA/COBERTURA_PARCIAL.
"""
from datetime import date, datetime, time, timezone
import hashlib
import json
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from pyproj import Transformer
from shapely import transform
from shapely.geometry import GeometryCollection, LineString, shape
from shapely.ops import unary_union
from shapely.strtree import STRtree

from app import db
from app.models import (
    AssociacaoInfraestruturaTrecho, ClassificacaoInfraestrutura, Trecho,
    VersaoInfraestrutura, agora_utc,
)

CRS_METRICO = "EPSG:31983"
VERSAO_REGRA = "us07-t02-espacial-1.0"
TOLERANCIA_PROXIMIDADE_M = 10.0
TOLERANCIA_COBERTURA = 0.99
COMPRIMENTO_MINIMO_M = 5.0
FUSO_MUNICIPIO = ZoneInfo("America/Sao_Paulo")


def _limite_consulta(quando):
    if isinstance(quando, date) and not isinstance(quando, datetime):
        return datetime.combine(quando, time.max, tzinfo=FUSO_MUNICIPIO).astimezone(timezone.utc)
    if quando.tzinfo is None:
        raise ValueError("a classificação exige data ou instante com fuso")
    return quando.astimezone(timezone.utc)


def versoes_vigentes(quando):
    """Escolhe o snapshot mais recente por fonte e categoria até a data pedida."""
    limite = _limite_consulta(quando)
    candidatas = VersaoInfraestrutura.query.filter(
        VersaoInfraestrutura.capturada_em <= limite,
    ).order_by(VersaoInfraestrutura.capturada_em.desc(), VersaoInfraestrutura.id.desc()).all()
    escolhidas = {}
    for versao in candidatas:
        categoria = versao.metadados.get("categoria")
        if categoria in {"CORREDOR", "FAIXA_EXCLUSIVA"}:
            escolhidas.setdefault((versao.fonte, categoria), versao)
    return sorted(escolhidas.values(), key=lambda v: (v.fonte, v.metadados["categoria"], v.id))


def _geometrias(versoes, transformar):
    versao_por_id = {versao.id: versao for versao in versoes}
    feicoes = ClassificacaoInfraestrutura.query.filter(
        ClassificacaoInfraestrutura.versao_id.in_(versao_por_id),
    ).order_by(ClassificacaoInfraestrutura.versao_id,
               ClassificacaoInfraestrutura.identificador_fonte).all() if versao_por_id else []
    resultado = []
    for feicao in feicoes:
        geom = transform(shape(feicao.geometria), transformar.transform, interleaved=False)
        resultado.append({
            "geometria": geom.buffer(TOLERANCIA_PROXIMIDADE_M),
            "versao_id": feicao.versao_id,
            "fonte": versao_por_id[feicao.versao_id].fonte,
            "identificador": feicao.identificador_fonte,
            "categoria": feicao.categoria,
        })
    return resultado


def _uniao(itens):
    return unary_union(itens) if itens else GeometryCollection()


def _classificar_linha(trecho, registros, arvore):
    candidatos = []
    if arvore is not None:
        candidatos = [int(i) for i in arvore.query(trecho)]
    por_fonte_categoria = {}
    for indice in candidatos:
        registro = registros[indice]
        por_fonte_categoria.setdefault((registro["fonte"], registro["categoria"]), []).append(
            registro["geometria"]
        )
    fonte_oficial = "GeoSampa"
    fonte_osm = "OpenStreetMap"
    gc = _uniao(por_fonte_categoria.get((fonte_oficial, "CORREDOR"), []))
    gf = _uniao(por_fonte_categoria.get((fonte_oficial, "FAIXA_EXCLUSIVA"), []))
    oficial = _uniao([g for chave, geoms in por_fonte_categoria.items()
                       if chave[0] == fonte_oficial for g in geoms])
    corredor_oficial = trecho.intersection(gc)
    faixa_oficial = trecho.intersection(gf).difference(corredor_oficial)
    osm_c = _uniao(por_fonte_categoria.get((fonte_osm, "CORREDOR"), []))
    osm_f = _uniao(por_fonte_categoria.get((fonte_osm, "FAIXA_EXCLUSIVA"), []))
    corredor_osm = trecho.intersection(osm_c).difference(oficial)
    faixa_osm = trecho.intersection(osm_f).difference(_uniao([oficial, corredor_osm]))
    corredor = _uniao([corredor_oficial, corredor_osm])
    faixa = _uniao([faixa_oficial, faixa_osm]).difference(corredor)
    conhecido = _uniao([corredor, faixa])
    restante = trecho.difference(conhecido)
    total = trecho.length
    fracoes = {
        "CORREDOR": min(1.0, corredor.length / total),
        "FAIXA_EXCLUSIVA": min(1.0, faixa.length / total),
        "SEM_COBERTURA": min(1.0, restante.length / total),
    }
    categorias = [categoria for categoria in ("CORREDOR", "FAIXA_EXCLUSIVA")
                  if fracoes[categoria] * total >= COMPRIMENTO_MINIMO_M]
    if not categorias:
        fracoes = {"CORREDOR": 0.0, "FAIXA_EXCLUSIVA": 0.0, "SEM_COBERTURA": 1.0}
    cobertura = fracoes["CORREDOR"] + fracoes["FAIXA_EXCLUSIVA"]
    if not categorias:
        classificacao = "SEM_COBERTURA"
    elif cobertura < TOLERANCIA_COBERTURA:
        classificacao = "COBERTURA_PARCIAL"
    elif len(categorias) > 1:
        classificacao = "MISTO"
    else:
        classificacao = categorias[0]
    feicoes = []
    for indice in candidatos:
        registro = registros[indice]
        intersecao = trecho.intersection(registro["geometria"])
        if registro["fonte"] == fonte_osm:
            intersecao = intersecao.difference(oficial)
            if registro["categoria"] == "FAIXA_EXCLUSIVA":
                intersecao = intersecao.difference(corredor_osm)
        metros = intersecao.length
        if metros < COMPRIMENTO_MINIMO_M:
            continue
        feicoes.append({"fonte": registro["fonte"], "versao_id": registro["versao_id"],
                        "identificador_fonte": registro["identificador"],
                        "categoria": registro["categoria"],
                        "comprimento_proximo_m": round(metros, 3)})
    regra = {"versao": VERSAO_REGRA, "crs_medicao": CRS_METRICO,
             "tolerancia_proximidade_m": TOLERANCIA_PROXIMIDADE_M,
             "comprimento_minimo_match_m": COMPRIMENTO_MINIMO_M,
             "limiar_cobertura_completa": TOLERANCIA_COBERTURA,
             "precedencia_fontes": [fonte_oficial, fonte_osm],
             "via_comum_confirmada": False,
             "observacao": "Sem cobertura positiva não equivale a via comum; falta camada completa de vias."}
    return classificacao, fracoes, categorias, feicoes, regra


def classificar_trechos(versao_gtfs_id, *, versoes=None, quando=None):
    """Grava uma associação para cada trecho, inclusive os sem cobertura."""
    if versoes is None:
        if quando is None:
            raise ValueError("informe versões explícitas ou uma data para selecionar snapshots")
        versoes = versoes_vigentes(quando)
    versoes = list(versoes)
    ids_versoes = sorted(v.id if isinstance(v, VersaoInfraestrutura) else str(v) for v in versoes)
    modelos = {v.id: v for v in VersaoInfraestrutura.query.filter(
        VersaoInfraestrutura.id.in_(ids_versoes),
    ).all()} if ids_versoes else {}
    if len(modelos) != len(ids_versoes):
        raise ValueError("uma ou mais versões de infraestrutura não existem")
    ids_versoes = sorted(modelos)
    conjunto = hashlib.sha256(json.dumps(ids_versoes, separators=(",", ":")).encode()).hexdigest()
    transformer = Transformer.from_crs("EPSG:4326", CRS_METRICO, always_xy=True)
    registros = _geometrias(list(modelos.values()), transformer)
    geometrias = [registro["geometria"] for registro in registros]
    arvore = STRtree(geometrias) if geometrias else None
    trechos = Trecho.query.filter_by(versao_gtfs_id=versao_gtfs_id).order_by(Trecho.id).all()
    agora = agora_utc()
    criadas = 0
    for trecho_model in trechos:
        existente = AssociacaoInfraestruturaTrecho.query.filter_by(
            trecho_id=trecho_model.id, versao_conjunto=conjunto,
        ).one_or_none()
        if existente:
            continue
        linha = LineString(trecho_model.geometria)
        linha = transform(linha, transformer.transform, interleaved=False)
        if linha.is_empty or linha.length <= 0:
            raise ValueError(f"trecho {trecho_model.id} tem geometria sem comprimento")
        classificacao, fracoes, categorias, feicoes, regra = _classificar_linha(
            linha, registros, arvore,
        )
        db.session.add(AssociacaoInfraestruturaTrecho(
            id=str(uuid5(NAMESPACE_URL, f"infra-trecho:{trecho_model.id}:{conjunto}")),
            trecho_id=trecho_model.id, versao_conjunto=conjunto,
            versoes_infraestrutura=ids_versoes, classificacao=classificacao,
            fracao_corredor=fracoes["CORREDOR"], fracao_faixa=fracoes["FAIXA_EXCLUSIVA"],
            fracao_sem_cobertura=fracoes["SEM_COBERTURA"], categorias_confirmadas=categorias,
            feicoes_associadas=feicoes, regra=regra, calculada_em=agora,
        ))
        criadas += 1
    db.session.commit()
    totais = {nome: AssociacaoInfraestruturaTrecho.query.filter_by(
        versao_conjunto=conjunto, classificacao=nome,
    ).join(Trecho).filter(Trecho.versao_gtfs_id == versao_gtfs_id).count()
              for nome in ("CORREDOR", "FAIXA_EXCLUSIVA", "MISTO", "COBERTURA_PARCIAL", "SEM_COBERTURA")}
    return {"versao_gtfs_id": versao_gtfs_id, "versoes_infraestrutura": ids_versoes,
            "versao_conjunto": conjunto, "trechos": len(trechos), "associacoes_novas": criadas,
            "classificacoes": totais,
            "sem_cobertura": totais["SEM_COBERTURA"] + totais["COBERTURA_PARCIAL"]}


def detalhar_trecho(trecho_id, *, quando):
    """Detalhe do trecho com classificação, frações, fonte e vigência escolhidas."""
    trecho = db.session.get(Trecho, trecho_id)
    if trecho is None:
        return None
    versoes = versoes_vigentes(quando)
    ids = sorted(v.id for v in versoes)
    conjunto = hashlib.sha256(json.dumps(ids, separators=(",", ":")).encode()).hexdigest()
    associacao = AssociacaoInfraestruturaTrecho.query.filter_by(
        trecho_id=trecho.id, versao_conjunto=conjunto,
    ).one_or_none()
    if associacao is None:
        return {"trecho_id": trecho.id, "classificacao": "NAO_PROCESSADO",
                "versoes_infraestrutura": ids, "mensagem": "Execute a associação para esta rede e versões."}
    por_id = {v.id: v for v in versoes}
    return {"trecho_id": trecho.id, "classificacao": associacao.classificacao,
            "fracao_corredor": associacao.fracao_corredor,
            "fracao_faixa_exclusiva": associacao.fracao_faixa,
            "fracao_sem_cobertura": associacao.fracao_sem_cobertura,
            "categorias_confirmadas": associacao.categorias_confirmadas,
            "fontes": [{"fonte": por_id[i].fonte, "url": por_id[i].url_fonte,
                        "versao_fonte": por_id[i].versao_fonte,
                        "capturada_em": por_id[i].capturada_em.isoformat()}
                       for i in associacao.versoes_infraestrutura],
            "feicoes_associadas": associacao.feicoes_associadas,
            "regra": associacao.regra, "calculada_em": associacao.calculada_em.isoformat()}
