"""Consulta pública de indicadores espaciais e agregações territoriais."""

from collections import defaultdict
import re

from flask import Blueprint, current_app, request
from sqlalchemy import false
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.datastructures import MultiDict

from app import db
from app.core.limites import IdentidadeIPInvalida, consumir_limite, identificar_ip
from app.indicadores.routes import ParametroInvalido, _erro, ler_filtros
from app.models import (
    IndicadorOperacional,
    Parada,
    ParadaTerritorial,
    Trecho,
    TrechoTerritorial,
    UnidadeTerritorial,
    VersaoTerritorial,
)

mapas_bp = Blueprint("mapas", __name__)

_FILTROS_COMUNS = {
    "linha", "sentido", "tipo", "indicador", "versao_gtfs_id",
    "inicio", "fim", "page", "page_size",
}


@mapas_bp.before_request
def limitar_consulta_mapa():
    if request.method not in ("GET", "HEAD"):
        return None
    try:
        consumo = consumir_limite(identificar_ip(request))
    except IdentidadeIPInvalida:
        return _erro("IP_NAO_IDENTIFICADO", "Não foi possível identificar o IP da conexão ou do proxy.", 400)
    except SQLAlchemyError as erro:
        current_app.logger.error("Limitador de mapas indisponível (%s)", type(erro).__name__)
        resposta, status = _erro("LIMITE_INDISPONIVEL", "Não foi possível verificar o limite. Tente novamente em instantes.", 503)
        return resposta, status, {"Retry-After": "5", "Cache-Control": "no-store"}
    if not consumo.permitido:
        return {"erro": {"codigo": "LIMITE_EXCEDIDO", "mensagem": "Limite de consultas por minuto por IP atingido.", "tentar_em_segundos": consumo.tentar_em}}, 429, {
            "Retry-After": str(consumo.tentar_em), "Cache-Control": "no-store",
        }
    request.environ["mapas.limite_cabecalhos"] = consumo.cabecalhos()


@mapas_bp.after_request
def informar_limite_mapa(resposta):
    cabecalhos = request.environ.get("mapas.limite_cabecalhos")
    if cabecalhos:
        resposta.headers.update(cabecalhos)
        resposta.headers["Cache-Control"] = "no-store"
    return resposta


def _aplicar_filtros(consulta, filtros):
    if filtros["linha"]:
        consulta = consulta.filter(IndicadorOperacional.linha_codigo == filtros["linha"])
    if filtros["sentido"]:
        consulta = consulta.filter(IndicadorOperacional.sentido == filtros["sentido"])
    if filtros["tipo"]:
        consulta = consulta.filter(IndicadorOperacional.indicador == filtros["tipo"])
    if filtros["versao_gtfs_id"]:
        consulta = consulta.filter(IndicadorOperacional.versao_gtfs_id == filtros["versao_gtfs_id"])
    if filtros["inicio"]:
        consulta = consulta.filter(IndicadorOperacional.periodo_fim > filtros["inicio"])
    if filtros["fim"]:
        if filtros["inicio"] and filtros["fim"] == filtros["inicio"]:
            consulta = consulta.filter(IndicadorOperacional.periodo_inicio <= filtros["fim"])
        else:
            consulta = consulta.filter(IndicadorOperacional.periodo_inicio < filtros["fim"])
    return consulta


def _versao_territorial(identificador):
    if identificador:
        return db.session.get(VersaoTerritorial, identificador)
    return VersaoTerritorial.query.order_by(
        VersaoTerritorial.capturada_em.desc(), VersaoTerritorial.id.desc()
    ).first()


def _propriedades(indicador):
    qualidade = indicador.qualidade or {}
    ausencia = None
    if indicador.valor is None:
        if indicador.contagem_denominador == 0:
            ausencia = "DENOMINADOR_ZERO"
        elif qualidade.get("dados_insuficientes"):
            ausencia = "DADOS_INSUFICIENTES"
        else:
            ausencia = "VALOR_INDISPONIVEL"
    return {
        "linha": indicador.linha_codigo,
        "sentido": indicador.sentido,
        "tipo": indicador.indicador,
        "valor": indicador.valor,
        "unidade": indicador.unidade,
        "contagens": {
            "numerador": indicador.contagem_numerador,
            "denominador": indicador.contagem_denominador,
        },
        "inicio": indicador.periodo_inicio.isoformat(),
        "fim": indicador.periodo_fim.isoformat(),
        "versao_gtfs_id": indicador.versao_gtfs_id,
        "versao_conjunto": indicador.versao_conjunto,
        "versao_correspondencia": indicador.versao_correspondencia,
        "versao_calculo": indicador.versao_calculo,
        "qualidade": qualidade,
        "ausencia": ausencia,
    }


def _geometria_parada(parada):
    return {"type": "Point", "coordinates": [float(parada.longitude), float(parada.latitude)]}


def _geometria_trecho(trecho):
    return {"type": "LineString", "coordinates": trecho.geometria}


def _feature(geometria, propriedades):
    return {"type": "Feature", "geometry": geometria, "properties": propriedades}


def _consulta_paradas(filtros, codigo, versao_territorial):
    consulta = db.session.query(IndicadorOperacional, Parada, ParadaTerritorial).join(
        Parada,
        (Parada.versao_gtfs_id == IndicadorOperacional.versao_gtfs_id)
        & (Parada.codigo == IndicadorOperacional.parada_codigo),
    ).outerjoin(
        ParadaTerritorial,
        (ParadaTerritorial.versao_gtfs_id == Parada.versao_gtfs_id)
        & (ParadaTerritorial.parada_codigo == Parada.codigo)
        & (ParadaTerritorial.versao_territorial_id == versao_territorial.id)
        if versao_territorial else false(),
    ).filter(IndicadorOperacional.parada_codigo.isnot(None))
    consulta = _aplicar_filtros(consulta, filtros)
    if codigo:
        consulta = consulta.filter(Parada.codigo == codigo)
    total = consulta.count()
    linhas = consulta.order_by(IndicadorOperacional.periodo_inicio, Parada.codigo,
                               IndicadorOperacional.id).offset(
        (filtros["page"] - 1) * filtros["page_size"]
    ).limit(filtros["page_size"]).all()
    features = []
    for indicador, parada, associacao in linhas:
        props = _propriedades(indicador)
        props.update(escopo="parada", parada=parada.codigo, nome=parada.nome)
        props.update(_props_territorio(associacao))
        features.append(_feature(_geometria_parada(parada), props))
    return features, total


def _consulta_trechos(filtros, codigo, versao_territorial):
    consulta = db.session.query(IndicadorOperacional, Trecho, TrechoTerritorial).join(
        Trecho,
        (Trecho.id == IndicadorOperacional.trecho_id)
        & (Trecho.versao_gtfs_id == IndicadorOperacional.versao_gtfs_id)
        & (Trecho.linha_codigo == IndicadorOperacional.linha_codigo),
    ).outerjoin(
        TrechoTerritorial,
        (TrechoTerritorial.trecho_id == Trecho.id)
        & (TrechoTerritorial.versao_gtfs_id == Trecho.versao_gtfs_id)
        & (TrechoTerritorial.linha_codigo == Trecho.linha_codigo)
        & (TrechoTerritorial.versao_territorial_id == versao_territorial.id)
        if versao_territorial else false(),
    ).filter(IndicadorOperacional.trecho_id.isnot(None))
    consulta = _aplicar_filtros(consulta, filtros)
    if codigo:
        consulta = consulta.filter(Trecho.id == codigo)
    total = consulta.count()
    linhas = consulta.order_by(IndicadorOperacional.periodo_inicio, Trecho.id,
                               IndicadorOperacional.id).offset(
        (filtros["page"] - 1) * filtros["page_size"]
    ).limit(filtros["page_size"]).all()
    features = []
    for indicador, trecho, associacao in linhas:
        props = _propriedades(indicador)
        props.update(
            escopo="trecho",
            trecho=trecho.id,
            origem=trecho.parada_origem_codigo,
            destino=trecho.parada_destino_codigo,
            fracao_distrito=associacao.fracao_distrito if associacao else None,
            fracao_subprefeitura=associacao.fracao_subprefeitura if associacao else None,
            cruza_limite=associacao.cruza_limite if associacao else None,
        )
        props.update(_props_territorio(associacao))
        features.append(_feature(_geometria_trecho(trecho), props))
    return features, total


def _props_territorio(associacao):
    if not associacao:
        return {
            "distrito_id": None,
            "subprefeitura_id": None,
            "ponto_na_fronteira": None,
            "territorio_ausente": True,
        }
    return {
        "distrito_id": associacao.distrito_id,
        "subprefeitura_id": associacao.subprefeitura_id,
        "ponto_na_fronteira": getattr(associacao, "ponto_na_fronteira", None),
        "territorio_ausente": False,
    }


_CHAVES_AGREGACAO = (
    "indicador", "unidade", "sentido", "periodo_inicio", "periodo_fim",
    "versao_gtfs_id", "versao_conjunto", "versao_correspondencia", "versao_calculo",
)


def _consulta_territorios(filtros, escopo, base, codigo, versao_territorial):
    if versao_territorial is None:
        return [], 0
    distrito = escopo == "distrito"
    chave_unidade = UnidadeTerritorial.id
    tabela_associacao = ParadaTerritorial if base == "parada" else TrechoTerritorial
    coluna_unidade = tabela_associacao.distrito_id if distrito else tabela_associacao.subprefeitura_id
    consulta = db.session.query(IndicadorOperacional, UnidadeTerritorial, tabela_associacao).select_from(
        tabela_associacao
    ).join(
        UnidadeTerritorial,
        (UnidadeTerritorial.id == coluna_unidade)
        & (UnidadeTerritorial.versao_id == tabela_associacao.versao_territorial_id)
        & (UnidadeTerritorial.tipo == ("DISTRITO" if distrito else "SUBPREFEITURA")),
    ).join(
        IndicadorOperacional,
        IndicadorOperacional.versao_gtfs_id == tabela_associacao.versao_gtfs_id,
    )
    if base == "parada":
        consulta = consulta.filter(
            IndicadorOperacional.versao_gtfs_id == ParadaTerritorial.versao_gtfs_id,
            IndicadorOperacional.parada_codigo == ParadaTerritorial.parada_codigo,
            IndicadorOperacional.parada_codigo.isnot(None),
        )
    else:
        consulta = consulta.filter(
            IndicadorOperacional.versao_gtfs_id == TrechoTerritorial.versao_gtfs_id,
            IndicadorOperacional.linha_codigo == TrechoTerritorial.linha_codigo,
            IndicadorOperacional.trecho_id == TrechoTerritorial.trecho_id,
            IndicadorOperacional.trecho_id.isnot(None),
        )
    consulta = consulta.filter(UnidadeTerritorial.versao_id == versao_territorial.id)
    consulta = _aplicar_filtros(consulta, filtros)
    if codigo:
        consulta = consulta.filter(UnidadeTerritorial.codigo == codigo)
    consulta = consulta.order_by(IndicadorOperacional.periodo_inicio,
                                IndicadorOperacional.id)
    grupos = defaultdict(lambda: {"numerador": 0, "denominador": 0,
                                  "tem_contagens": True, "registros": 0})
    unidades = {}
    for indicador, unidade, _ in consulta.yield_per(1000):
        chave = (unidade.id,) + tuple(getattr(indicador, nome) for nome in _CHAVES_AGREGACAO)
        agregado = grupos[chave]
        if indicador.contagem_numerador is None or indicador.contagem_denominador is None:
            agregado["tem_contagens"] = False
        else:
            agregado["numerador"] += indicador.contagem_numerador
            agregado["denominador"] += indicador.contagem_denominador
        agregado["registros"] += 1
        unidades[unidade.id] = unidade

    features = []
    for chave, agregado in grupos.items():
        unidade = unidades[chave[0]]
        valores = dict(zip(_CHAVES_AGREGACAO, chave[1:]))
        n = agregado["numerador"] if agregado["tem_contagens"] else None
        d = agregado["denominador"] if agregado["tem_contagens"] else None
        valor = (100 * n / d) if n is not None and d else None
        props = {
            "codigo": unidade.codigo,
            "nome": unidade.nome,
            "escopo": escopo,
            "base_agregacao": base,
            "tipo": valores["indicador"],
            "unidade": valores["unidade"],
            "sentido": valores["sentido"],
            "valor": valor,
            "contagens": {"numerador": n, "denominador": d},
            "registros_agregados": agregado["registros"],
            "inicio": valores["periodo_inicio"].isoformat(),
            "fim": valores["periodo_fim"].isoformat(),
            "versao_gtfs_id": valores["versao_gtfs_id"],
            "versao_territorial_id": unidade.versao_id,
            "versao_conjunto": valores["versao_conjunto"],
            "versao_correspondencia": valores["versao_correspondencia"],
            "versao_calculo": valores["versao_calculo"],
            "qualidade": {"registros_agregados": agregado["registros"]},
            "ausencia": "DENOMINADOR_ZERO" if d == 0 else ("CONTAGENS_INDISPONIVEIS" if d is None else None),
        }
        features.append(_feature(unidade.geometria, props))
    # Se há ao menos uma medida, incluir também unidades sem observação para
    # o mapa poder diferenciá-las de unidades que ficaram fora do recorte.
    if features:
        tipo_unidade = "DISTRITO" if distrito else "SUBPREFEITURA"
        unidades_sem_dado = UnidadeTerritorial.query.filter_by(
            versao_id=versao_territorial.id, tipo=tipo_unidade,
        )
        if codigo:
            unidades_sem_dado = unidades_sem_dado.filter_by(codigo=codigo)
        for unidade in unidades_sem_dado.order_by(UnidadeTerritorial.codigo):
            if unidade.id in unidades:
                continue
            features.append(_feature(unidade.geometria, {
                "codigo": unidade.codigo,
                "nome": unidade.nome,
                "escopo": escopo,
                "base_agregacao": base,
                "tipo": None,
                "unidade": None,
                "sentido": None,
                "valor": None,
                "contagens": {"numerador": None, "denominador": None},
                "registros_agregados": 0,
                "versao_territorial_id": unidade.versao_id,
                "ausencia": "SEM_DADOS",
            }))
    total = len(features)
    inicio = (filtros["page"] - 1) * filtros["page_size"]
    return features[inicio:inicio + filtros["page_size"]], total


@mapas_bp.get("/mapas/indicadores")
def consultar_mapa_indicadores():
    """Retorna FeatureCollection GeoJSON de indicadores publicados."""
    escopo = request.args.get("escopo", "").strip().lower()
    if escopo not in {"parada", "trecho", "distrito", "subprefeitura"}:
        return _erro("PARAMETRO_INVALIDO", "escopo deve ser parada, trecho, distrito ou subprefeitura.", 400, "escopo")
    base = request.args.get("base", "").strip().lower() or None
    if escopo in {"distrito", "subprefeitura"} and base not in {"parada", "trecho"}:
        return _erro("PARAMETRO_INVALIDO", "Informe base=parada ou base=trecho para agregar por território.", 400, "base")
    if escopo in {"parada", "trecho"} and base is not None:
        return _erro("PARAMETRO_INVALIDO", "base só se aplica a distrito ou subprefeitura.", 400, "base")
    permitidos = _FILTROS_COMUNS | {"escopo", "base", "codigo", "versao_territorial_id"}
    for campo in request.args:
        if campo not in permitidos:
            return _erro("PARAMETRO_INVALIDO", "Parâmetro não reconhecido.", 400, campo)
        if len(request.args.getlist(campo)) != 1:
            return _erro("PARAMETRO_INVALIDO", "Informe cada parâmetro uma única vez.", 400, campo)
    if "codigo" in request.args and (
        not request.args["codigo"].strip()
        or len(request.args["codigo"].strip()) > 64
        or any(ord(char) < 32 or ord(char) == 127 for char in request.args["codigo"])
    ):
        return _erro("PARAMETRO_INVALIDO", "Informe um código com até 64 caracteres.", 400, "codigo")
    versao_id = request.args.get("versao_territorial_id", "").strip() or None
    if versao_id and not re.fullmatch(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
        versao_id,
    ):
        return _erro("PARAMETRO_INVALIDO", "Identificador territorial inválido.", 400, "versao_territorial_id")
    try:
        filtros = ler_filtros(MultiDict((k, v) for k, v in request.args.items(multi=True)
                                       if k in _FILTROS_COMUNS))
    except ParametroInvalido as erro:
        return _erro("PARAMETRO_INVALIDO", str(erro), 400, erro.campo)
    if filtros["versao_gtfs_id"] is None:
        return _erro("PARAMETRO_INVALIDO", "Informe versao_gtfs_id para fixar a rede e evitar misturar versões.", 400, "versao_gtfs_id")
    try:
        versao = _versao_territorial(versao_id)
        codigo = request.args.get("codigo", "").strip() or None
        if escopo == "parada":
            features, total = _consulta_paradas(filtros, codigo, versao)
        elif escopo == "trecho":
            features, total = _consulta_trechos(filtros, codigo, versao)
        else:
            if versao is None:
                features, total = [], 0
            else:
                features, total = _consulta_territorios(filtros, escopo, base, codigo, versao)
        legenda = {
            "tipo": "valor publicado; agregações territoriais recalculadas pelas contagens",
            "unidade": "a unidade original do indicador",
            "ausencia": {
                "DENOMINADOR_ZERO": "denominador zero; valor null",
                "CONTAGENS_INDISPONIVEIS": "contagens insuficientes para agregar",
                "DADOS_INSUFICIENTES": "publicação sinalizou insuficiência de dados",
                "VERSAO_TERRITORIAL_INDISPONIVEL": "não há versão de limites disponível",
                "SEM_DADOS": "nenhum indicador publicado atende aos filtros",
            },
        }
        resposta = current_app.json.response({
            "type": "FeatureCollection",
            "features": features,
            "legenda": legenda,
            "versao_territorial": ({"id": versao.id, "fonte": versao.fonte,
                                    "versao_fonte": versao.versao_fonte} if versao else None),
            "ausencia": "VERSAO_TERRITORIAL_INDISPONIVEL" if versao is None else ("SEM_DADOS" if not features else None),
            "paginacao": {"page": filtros["page"], "page_size": filtros["page_size"], "total": total},
        })
        resposta.mimetype = "application/geo+json"
        return resposta, 200
    except SQLAlchemyError as erro:
        db.session.rollback()
        current_app.logger.error("Falha na consulta de mapa (%s)", type(erro).__name__)
        return _erro("CONSULTA_INDISPONIVEL", "Não foi possível consultar o mapa de indicadores.", 503)
