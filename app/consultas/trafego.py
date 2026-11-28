"""QueryService — tráfego por trecho em janelas de 15 min (US08-T03, docs/US08-T03.md).

Só lê o que a US08-T02 gravou em ``MedicaoContexto`` (tipo TRAFEGO); nenhuma
velocidade é calculada aqui. Fonte ativa: velocidade observada dos ônibus, um
*proxy* de tráfego. O Waze não é consultado: não há acordo (política de fontes).

Cada conjunto de versões (método da média + GTFS, estimativa, reconstrução e
correspondência) forma uma série separada; séries nunca se misturam. Toda
janela de 15 min inteira do período aparece em cada série: sem registro, a
velocidade é ``null`` com motivo, nunca zero.
"""

import math
from datetime import datetime, timedelta, timezone

from app import db
from app.contexto.trafego import (
    DESCRICAO_FONTE, FONTE_AGREGADO, TIPO_CONTEXTO, UNIDADES_AGREGADO,
)
from app.core.tempo import FUSO
from app.gtfs.service import VersaoGTFSAmbigua, VersaoGTFSInexistente, versao_vigente
from app.models import MedicaoContexto, Trecho

UTC = timezone.utc
JANELA = timedelta(minutes=15)
POLITICA_FONTES = {
    "fonte_ativa": FONTE_AGREGADO,
    "sem_acordo_waze": ("Obrigatório: velocidade média observada dos ônibus por trecho em janelas "
                        "de 15 min (US08-T01/T02)."),
    "com_acordo_waze": ("Futuro: eventos do Waze a cada 2 min, em ingestão separada, após acordo "
                        "e novas subtarefas autorizadas. Não implementado."),
    "waze_integrado": False,
}
SEM_REGISTRO = "sem_registro"  # janela não processada ou processada sem nenhuma amostra (T02 não grava)


class TrechoInexistente(LookupError):
    pass


def _iso(instante):
    return instante.astimezone(UTC).isoformat().replace("+00:00", "Z")


def janelas_inteiras(inicio, fim):
    """Inícios das janelas [a, a+15 min) da grade de 15 min contidas em [inicio, fim).

    São Paulo tem deslocamento de horas inteiras: a grade é a mesma em UTC e no
    horário local.
    """
    passo = JANELA.total_seconds()
    atual = datetime.fromtimestamp(math.ceil(inicio.timestamp() / passo) * passo, UTC)
    resultado = []
    while atual + JANELA <= fim:
        resultado.append(atual)
        atual += JANELA
    return resultado


def _conjunto(registro):
    versoes = {k: v for k, v in registro.dados.get("versoes", {}).items() if k != "calculo_amostras"}
    return registro.versao_fonte, tuple(sorted(versoes.items()))


def _janela(inicio, registro):
    base = {"inicio": _iso(inicio), "fim": _iso(inicio + JANELA)}
    if registro is None:
        return {**base, "velocidade_media_kmh": None, "motivo": SEM_REGISTRO, "amostras_validas": 0,
                "amostras_descartadas": 0, "descartes_por_motivo": {}, "viagens_validas": 0,
                "cobertura": None, "qualidade_coleta": None, "calculo_amostras": [],
                "calculado_em": None}
    d = registro.dados
    coleta = (d.get("qualidade") or {}).get("falhas_coleta") or {}
    return {
        **base, "velocidade_media_kmh": d.get("velocidade_media_kmh"), "motivo": d.get("motivo"),
        "amostras_validas": d.get("amostras_validas"), "amostras_descartadas": d.get("amostras_descartadas"),
        "descartes_por_motivo": d.get("descartes_por_motivo"), "viagens_validas": d.get("viagens_validas"),
        "candidatos_kmh": d.get("candidatos_kmh"),
        "cobertura": {"proporcao_validas": (d.get("qualidade") or {}).get("proporcao_validas")},
        "qualidade_coleta": {k: coleta.get(k) for k in ("situacao", "motivo", "percentual_falhas",
                                                         "incompleto")} if coleta else None,
        "calculo_amostras": d.get("versoes", {}).get("calculo_amostras", []),
        "calculado_em": _iso(registro.coletada_em),
    }


def consultar_trafego_trecho(trecho_id, inicio, fim, versao_fonte=None):
    trecho = db.session.get(Trecho, trecho_id)
    if trecho is None:
        raise TrechoInexistente(trecho_id)
    M = MedicaoContexto
    consulta = M.query.filter(M.tipo == TIPO_CONTEXTO, M.fonte == FONTE_AGREGADO,
                              M.trecho_id == trecho_id, M.inicio >= inicio, M.fim <= fim)
    if versao_fonte:
        consulta = consulta.filter(M.versao_fonte == versao_fonte)
    grade = janelas_inteiras(inicio, fim)
    series = {}
    for registro in consulta.order_by(M.inicio, M.id):
        series.setdefault(_conjunto(registro), {})[registro.inicio] = registro
    corpo = {
        "trecho": {"id": trecho.id, "versao_gtfs_id": trecho.versao_gtfs_id,
                   "linha": trecho.linha_codigo, "sentido": trecho.sentido, "ordem": trecho.ordem},
        "periodo": {"inicio": _iso(inicio), "fim": _iso(fim)},
        "fonte": {"codigo": FONTE_AGREGADO, "tipo": "PROXY_TRAFEGO_VELOCIDADE_ONIBUS",
                  "descricao": DESCRICAO_FONTE, "politica": POLITICA_FONTES},
        "unidades": {"velocidade_media_kmh": UNIDADES_AGREGADO["velocidade_media_kmh"],
                     "candidatos_kmh": UNIDADES_AGREGADO["candidatos_kmh"]},
        "convencao": ("Janelas [inicio, fim) de 15 min, em UTC, alinhadas ao relógio de São Paulo. "
                      "null significa velocidade indisponível (ver motivo), nunca zero."),
        "series": [],
        "motivo": None,
    }
    for (versao, versoes), registros in sorted(series.items()):
        exemplo = next(iter(registros.values())).dados
        corpo["series"].append({
            "versao_fonte": versao, "metodo_media": exemplo.get("metodo_media"),
            "atribuicao_janela": exemplo.get("atribuicao_janela"), "versoes": dict(versoes),
            "janelas": [_janela(a, registros.get(a)) for a in grade],
        })
    if not corpo["series"]:
        corpo["motivo"] = "sem_resultados_persistidos"
    return corpo


def consultar_mapa_trafego(instante, linha, sentido=None):
    """Trânsito no mapa ao vivo (US08 + US22): trechos da linha, na versão GTFS vigente no dia de
    ``instante`` (São Paulo), com a velocidade da janela de 15 min que contém ``instante``.

    FeatureCollection GeoJSON com uma LineString por trecho. Só lê o que a US08-T02 gravou; se
    houver mais de um cálculo para a mesma janela, vale o mais recente. Sem registro, a velocidade
    é null com motivo, nunca zero.
    """
    passo = int(JANELA.total_seconds())
    inicio = datetime.fromtimestamp(int(instante.astimezone(UTC).timestamp()) // passo * passo, UTC)
    corpo = {
        "type": "FeatureCollection", "features": [], "linha": linha, "sentido": sentido,
        "janela": {"inicio": _iso(inicio), "fim": _iso(inicio + JANELA)},
        "fonte": {"codigo": FONTE_AGREGADO, "tipo": "PROXY_TRAFEGO_VELOCIDADE_ONIBUS", "descricao": DESCRICAO_FONTE},
        "unidades": {"velocidade_media_kmh": UNIDADES_AGREGADO["velocidade_media_kmh"]},
        "versao_gtfs_id": None, "motivo": None,
    }
    try:
        versao = versao_vigente(instante.astimezone(FUSO).date())
    except VersaoGTFSInexistente:
        corpo["motivo"] = "sem_versao_gtfs_vigente"
        return corpo
    except VersaoGTFSAmbigua:
        corpo["motivo"] = "versao_gtfs_ambigua"
        return corpo
    corpo["versao_gtfs_id"] = versao.id
    consulta = Trecho.query.filter_by(versao_gtfs_id=versao.id, linha_codigo=linha)
    if sentido is not None:
        consulta = consulta.filter_by(sentido=sentido)
    trechos = consulta.order_by(Trecho.sentido, Trecho.trajeto, Trecho.ordem).all()
    if not trechos:
        corpo["motivo"] = "linha_sem_trechos"
        return corpo
    M = MedicaoContexto
    recentes = {}
    for registro in M.query.filter(M.tipo == TIPO_CONTEXTO, M.fonte == FONTE_AGREGADO,
                                   M.trecho_id.in_([t.id for t in trechos]), M.inicio == inicio):
        atual = recentes.get(registro.trecho_id)
        if atual is None or (registro.coletada_em, registro.id) > (atual.coletada_em, atual.id):
            recentes[registro.trecho_id] = registro
    for trecho in trechos:
        registro = recentes.get(trecho.id)
        dados = registro.dados if registro else {}
        corpo["features"].append({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": [list(p) for p in trecho.geometria]},
            "properties": {
                "trecho_id": trecho.id, "linha": trecho.linha_codigo, "sentido": trecho.sentido,
                "trajeto": trecho.trajeto, "ordem": trecho.ordem,
                "velocidade_media_kmh": dados.get("velocidade_media_kmh"),
                "motivo": dados.get("motivo") if registro else SEM_REGISTRO,
                "versao_fonte": registro.versao_fonte if registro else None,
            },
        })
    return corpo
