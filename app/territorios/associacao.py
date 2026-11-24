"""Deterministic one-territory assignment for stops and route segments."""

from dataclasses import dataclass

from pyproj import Transformer
from shapely import transform
from shapely.geometry import LineString, Point, shape
from shapely.strtree import STRtree

from app import db
from app.models import (
    Parada,
    ParadaTerritorial,
    Trecho,
    TrechoTerritorial,
    UnidadeTerritorial,
    VersaoGTFS,
    VersaoTerritorial,
)
from app.territorios.importacao import CRS_DESTINO, VERSAO_REGRA

CRS_MEDICAO = "EPSG:31983"
TOLERANCIA_COMPRIMENTO_M = 0.001
TOLERANCIA_FRONTEIRA_M = 0.01


@dataclass(frozen=True)
class ResultadoAssociacaoTerritorial:
    paradas_associadas: int
    paradas_sem_territorio: int
    paradas_na_fronteira: int
    trechos_associados: int
    trechos_sem_territorio: int
    trechos_que_cruzam_limite: int


def _ordem_codigo(unidade):
    codigo = unidade.codigo
    return (0, int(codigo)) if codigo.isdigit() else (1, codigo.casefold())


def _indice(versao_id, tipo, para_metros):
    unidades = (
        UnidadeTerritorial.query.filter_by(versao_id=versao_id, tipo=tipo)
        .order_by(UnidadeTerritorial.codigo)
        .all()
    )
    geometrias = [
        transform(shape(unidade.geometria), para_metros.transform, interleaved=False)
        for unidade in unidades
    ]
    return unidades, geometrias, STRtree(geometrias)


def _candidatos_arvore(arvore, geometria):
    return [int(indice) for indice in arvore.query(geometria)]


def _atribuir_parada(parada, distritos, geometrias, arvore, para_metros):
    ponto = transform(
        Point(float(parada.longitude), float(parada.latitude)),
        para_metros.transform,
        interleaved=False,
    )
    cobertos = [
        indice for indice in _candidatos_arvore(arvore, ponto.buffer(TOLERANCIA_FRONTEIRA_M))
        if geometrias[indice].covers(ponto)
        or geometrias[indice].boundary.distance(ponto) <= TOLERANCIA_FRONTEIRA_M
    ]
    if not cobertos:
        return None
    escolhido = min((distritos[i] for i in cobertos), key=_ordem_codigo)
    na_fronteira = len(cobertos) > 1 or any(
        geometrias[i].boundary.distance(ponto) <= TOLERANCIA_FRONTEIRA_M for i in cobertos
    )
    return ParadaTerritorial(
        versao_gtfs_id=parada.versao_gtfs_id,
        parada_codigo=parada.codigo,
        versao_territorial_id=escolhido.versao_id,
        distrito_id=escolhido.id,
        subprefeitura_id=escolhido.subprefeitura_id,
        ponto_na_fronteira=na_fronteira,
        qualidade={
            "regra": "COBERTURA_PONTUAL_CODIGO_CRESCENTE",
            "candidatos_distrito": len(cobertos),
            "versao_regra": VERSAO_REGRA,
        },
    )


def _sobreposicoes(linha, geometrias, arvore):
    comprimentos = {}
    for indice in _candidatos_arvore(arvore, linha):
        comprimento = linha.intersection(geometrias[indice]).length
        if comprimento > TOLERANCIA_COMPRIMENTO_M:
            comprimentos[indice] = comprimento
    return comprimentos


def _atribuir_trecho(trecho, distritos, geometrias_distritos, arvore_distritos,
                     subprefeituras, geometrias_subprefeituras, arvore_subprefeituras,
                     para_metros):
    linha = transform(LineString(trecho.geometria), para_metros.transform, interleaved=False)
    comprimento_total = linha.length
    if comprimento_total <= TOLERANCIA_COMPRIMENTO_M:
        return None

    por_subprefeitura = _sobreposicoes(
        linha, geometrias_subprefeituras, arvore_subprefeituras,
    )
    por_distrito = _sobreposicoes(linha, geometrias_distritos, arvore_distritos)
    if not por_subprefeitura or not por_distrito:
        return None

    # Primeiro seleciona a subprefeitura com maior extensão do trecho; em
    # empate usa o menor código. Depois seleciona o distrito de maior extensão
    # dentro dessa subprefeitura, mantendo a hierarquia consistente.
    indice_subprefeitura = min(
        por_subprefeitura,
        key=lambda i: (-por_subprefeitura[i], _ordem_codigo(subprefeituras[i])),
    )
    subprefeitura = subprefeituras[indice_subprefeitura]
    distritos_da_subprefeitura = [
        i for i in por_distrito
        if distritos[i].subprefeitura_id == subprefeitura.id
    ]
    if not distritos_da_subprefeitura:
        return None
    indice_distrito = min(
        distritos_da_subprefeitura,
        key=lambda i: (-por_distrito[i], _ordem_codigo(distritos[i])),
    )
    distrito = distritos[indice_distrito]
    comprimento_distrito = por_distrito[indice_distrito]
    comprimento_subprefeitura = por_subprefeitura[indice_subprefeitura]
    cruza = len(por_distrito) > 1 or len(por_subprefeitura) > 1

    return TrechoTerritorial(
        trecho_id=trecho.id,
        versao_gtfs_id=trecho.versao_gtfs_id,
        linha_codigo=trecho.linha_codigo,
        versao_territorial_id=distrito.versao_id,
        distrito_id=distrito.id,
        subprefeitura_id=subprefeitura.id,
        fracao_distrito=min(1.0, comprimento_distrito / comprimento_total),
        fracao_subprefeitura=min(1.0, comprimento_subprefeitura / comprimento_total),
        cruza_limite=cruza,
        qualidade={
            "regra": "MAIOR_SOBREPOSICAO_COM_DESEMPATE_POR_CODIGO",
            "candidatos_distrito": len(por_distrito),
            "candidatos_subprefeitura": len(por_subprefeitura),
            "comprimento_total_m": comprimento_total,
            "versao_regra": VERSAO_REGRA,
        },
    )


def associar_rede_a_territorios(versao_gtfs_id, versao_territorial_id):
    """Assign each stop and segment once; report uncovered network elements.

    Stop boundary points use the lowest numeric official district code. A
    segment belongs to the district/subprefecture with the greatest projected
    length overlap. Fractions and crossing flags remain available for audits.
    """
    if db.session.get(VersaoGTFS, versao_gtfs_id) is None:
        raise ValueError("versão GTFS não encontrada")
    if db.session.get(VersaoTerritorial, versao_territorial_id) is None:
        raise ValueError("versão territorial não encontrada")

    para_metros = Transformer.from_crs(CRS_DESTINO, CRS_MEDICAO, always_xy=True)
    distritos, geometrias_distritos, arvore_distritos = _indice(
        versao_territorial_id, "DISTRITO", para_metros,
    )
    subprefeituras, geometrias_subprefeituras, arvore_subprefeituras = _indice(
        versao_territorial_id, "SUBPREFEITURA", para_metros,
    )
    if not distritos or not subprefeituras:
        raise ValueError("a versão territorial precisa conter distritos e subprefeituras")

    try:
        ParadaTerritorial.query.filter_by(
            versao_gtfs_id=versao_gtfs_id,
            versao_territorial_id=versao_territorial_id,
        ).delete(synchronize_session=False)
        TrechoTerritorial.query.filter_by(
            versao_gtfs_id=versao_gtfs_id,
            versao_territorial_id=versao_territorial_id,
        ).delete(synchronize_session=False)

        paradas = Parada.query.filter_by(versao_gtfs_id=versao_gtfs_id).all()
        paradas_associadas = paradas_na_fronteira = 0
        for parada in paradas:
            associacao = _atribuir_parada(
                parada, distritos, geometrias_distritos, arvore_distritos, para_metros,
            )
            if associacao is not None:
                db.session.add(associacao)
                paradas_associadas += 1
                paradas_na_fronteira += associacao.ponto_na_fronteira

        trechos = Trecho.query.filter_by(versao_gtfs_id=versao_gtfs_id).all()
        trechos_associados = trechos_que_cruzam_limite = 0
        for trecho in trechos:
            associacao = _atribuir_trecho(
                trecho, distritos, geometrias_distritos, arvore_distritos,
                subprefeituras, geometrias_subprefeituras, arvore_subprefeituras,
                para_metros,
            )
            if associacao is not None:
                db.session.add(associacao)
                trechos_associados += 1
                trechos_que_cruzam_limite += associacao.cruza_limite

        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

    return ResultadoAssociacaoTerritorial(
        paradas_associadas=paradas_associadas,
        paradas_sem_territorio=len(paradas) - paradas_associadas,
        paradas_na_fronteira=paradas_na_fronteira,
        trechos_associados=trechos_associados,
        trechos_sem_territorio=len(trechos) - trechos_associados,
        trechos_que_cruzam_limite=trechos_que_cruzam_limite,
    )
