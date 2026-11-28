"""QueryService — clima por trecho e cobertura mensal (US06-T03/T04).

A medição de clima de um trecho numa janela é derivada, sem tabela própria:
trecho → estação usada (`TrechoEstacaoClima`) → observação da estação cuja
janela nativa coincide (`ObservacaoClima`). Assim a consulta nunca cria valor:
janela sem observação devolve `null` com motivo, e não chuva zero.

Janelas: grade nativa da fonte em UTC (1 h no INMET, 15 min no CGE). Cada
janela é (fim - intervalo, fim]; só entram janelas inteiras dentro do período
e que já terminaram.
"""

from calendar import monthrange
from collections import Counter
from datetime import datetime, timedelta, timezone

from sqlalchemy import func

from app import db
from app.core.tempo import FUSO
from app.integrations.clima import INTERVALO_CGE_SEG, INTERVALO_INMET_SEG
from app.models import (
    ColetaClima, EstacaoMeteorologica, ObservacaoClima, Trecho, TrechoEstacaoClima, agora_utc,
)

UTC = timezone.utc
INTERVALO_FONTE = {"INMET": INTERVALO_INMET_SEG, "CGE": INTERVALO_CGE_SEG}
META_COBERTURA_PCT = 95.0
UNIDADES = {"chuva": "mm", "temperatura": "°C"}
LIMITE_SEM_MEDICAO_LISTADOS = 100
DENOMINADOR = ("Janelas nativas da fonte (INMET 1 h, CGE 15 min, em UTC) inteiramente dentro do "
               "período, desde o início de operação da estação. Cada janela conta uma vez, por "
               "variável; observações repetidas ou fora da grade não somam. Períodos de "
               "indisponibilidade da fonte não são descontados do denominador.")


class VinculoClimaInexistente(LookupError):
    """O trecho não existe ou ainda não tem vínculo calculado para a fonte."""


def _iso(instante):
    return instante.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _numero(valor):
    return None if valor is None else float(valor)


def fins_de_janela(inicio, fim, intervalo_seg):
    """Fins das janelas (fim - intervalo, fim] inteiramente dentro de [inicio, fim]."""
    passo = timedelta(seconds=intervalo_seg)
    segundos = int(inicio.astimezone(UTC).timestamp())
    primeiro = -(-segundos // intervalo_seg) * intervalo_seg + intervalo_seg
    atual = datetime.fromtimestamp(primeiro, UTC)
    while atual <= fim:
        yield atual
        atual += passo


def _observacoes(estacao_id, inicio, fim):
    linhas = ObservacaoClima.query.filter(
        ObservacaoClima.estacao_id == estacao_id,
        ObservacaoClima.instante > inicio, ObservacaoClima.instante <= fim,
    ).all()
    return {o.instante: o for o in linhas}


def _cobertura(janelas):
    esperadas = len(janelas)
    chuva = sum(1 for j in janelas if j["chuva_mm"] is not None)
    temperatura = sum(1 for j in janelas if j["temperatura_c"] is not None)
    sem_medicao = [j["fim"] for j in janelas if j["motivo"]]
    sem_observacao = sum(1 for j in janelas if j["chuva_mm"] is None and j["temperatura_c"] is None)

    def pct(recebidas):
        return None if esperadas == 0 else round(100 * recebidas / esperadas, 2)

    return {
        "janelas_esperadas": esperadas,
        "chuva_recebidas": chuva, "chuva_pct": pct(chuva),
        "temperatura_recebidas": temperatura, "temperatura_pct": pct(temperatura),
        # Janelas com alguma variável ausente; `janelas_sem_observacao`: sem nenhuma das duas.
        "janelas_sem_medicao": len(sem_medicao),
        "janelas_sem_observacao": sem_observacao,
        "meta_pct": META_COBERTURA_PCT,
    }


def _janelas(estacao, situacao, inicio, fim, intervalo_seg):
    observacoes = _observacoes(estacao.id, inicio, fim) if situacao == "ASSOCIADO" else {}
    janelas = []
    for fim_janela in fins_de_janela(inicio, fim, intervalo_seg):
        obs = observacoes.get(fim_janela)
        if situacao != "ASSOCIADO":
            motivo = situacao
        elif obs is None:
            motivo = "SEM_OBSERVACAO"
        elif obs.chuva is None and obs.temperatura is None:
            motivo = "VALORES_NAO_INFORMADOS"
        elif obs.chuva is None:
            motivo = "CHUVA_NAO_INFORMADA"
        elif obs.temperatura is None:
            motivo = "TEMPERATURA_NAO_INFORMADA"
        else:
            motivo = None
        janelas.append({
            "inicio": _iso(fim_janela - timedelta(seconds=intervalo_seg)),
            "fim": _iso(fim_janela),
            "chuva_mm": _numero(obs.chuva) if obs else None,
            "temperatura_c": _numero(obs.temperatura) if obs else None,
            "motivo": motivo,
        })
    return janelas


def consultar_clima_trecho(trecho_id, inicio, fim, fonte="INMET", agora=None):
    agora = agora or agora_utc()
    vinculo = db.session.get(TrechoEstacaoClima, (trecho_id, fonte))
    if vinculo is None:
        raise VinculoClimaInexistente(trecho_id)
    trecho = db.session.get(Trecho, trecho_id)
    estacao = vinculo.estacao
    intervalo = estacao.intervalo_seg if estacao else INTERVALO_FONTE[fonte]
    limite = min(fim, agora)
    if estacao is not None:
        janelas = _janelas(estacao, vinculo.situacao, inicio, limite, intervalo)
    else:
        janelas = [{
            "inicio": _iso(f - timedelta(seconds=intervalo)), "fim": _iso(f),
            "chuva_mm": None, "temperatura_c": None, "motivo": vinculo.situacao,
        } for f in fins_de_janela(inicio, limite, intervalo)]
    return {
        "trecho": {"id": trecho.id, "versao_gtfs_id": trecho.versao_gtfs_id,
                   "linha": trecho.linha_codigo, "sentido": trecho.sentido, "ordem": trecho.ordem},
        "vinculo": {
            "fonte": fonte, "situacao": vinculo.situacao, "distancia_m": vinculo.distancia_m,
            "raio_maximo_m": vinculo.raio_maximo_m, "crs_medicao": vinculo.crs_medicao,
            "versao_regra": vinculo.versao_regra, "calculado_em": _iso(vinculo.calculado_em),
        },
        "estacao": None if estacao is None else {
            "codigo": estacao.codigo, "nome": estacao.nome,
            "latitude": float(estacao.latitude), "longitude": float(estacao.longitude),
            "intervalo_seg": estacao.intervalo_seg,
        },
        "periodo": {"inicio": _iso(inicio), "fim": _iso(fim)},
        "unidades": UNIDADES,
        "convencao": ("Instantes em UTC. chuva_mm é o total acumulado na janela (inicio, fim]; "
                      "temperatura_c é a leitura instantânea no fim da janela. null significa "
                      "sem medição, nunca ausência de chuva."),
        "janelas": janelas,
        "cobertura": _cobertura(janelas),
    }


def _limites_mes(ano, mes):
    """Mês civil no horário de São Paulo, convertido para UTC."""
    inicio = datetime(ano, mes, 1, tzinfo=FUSO)
    fim = inicio + timedelta(days=monthrange(ano, mes)[1])
    fim = datetime(fim.year, fim.month, fim.day, tzinfo=FUSO)
    return inicio.astimezone(UTC), fim.astimezone(UTC)


def cobertura_mensal(ano, mes, fonte="INMET", agora=None):
    """Observações recebidas x esperadas por estação, com a meta de 95%.

    Mês ainda em curso ou estação que começou a operar no meio do mês é
    identificado como AMOSTRA: o percentual não vale como resultado mensal.
    """
    agora = agora or agora_utc()
    inicio_mes, fim_mes = _limites_mes(ano, mes)
    usadas = {e for (e,) in db.session.query(TrechoEstacaoClima.estacao_id).filter(
        TrechoEstacaoClima.fonte == fonte, TrechoEstacaoClima.situacao == "ASSOCIADO").distinct()}
    consulta = EstacaoMeteorologica.query.filter_by(fonte=fonte)
    if usadas:
        consulta = consulta.filter(EstacaoMeteorologica.id.in_(usadas))
    resultado = []
    for estacao in consulta.order_by(EstacaoMeteorologica.codigo):
        inicio = inicio_mes
        if estacao.operacao_inicio:
            inicio = max(inicio, datetime.combine(estacao.operacao_inicio, datetime.min.time(), UTC))
        fim = min(fim_mes, agora)
        janelas = _janelas(estacao, "ASSOCIADO", inicio, fim, estacao.intervalo_seg)
        sem_medicao = [j["fim"] for j in janelas if j["motivo"]]
        cobertura = _cobertura(janelas)
        amostra = fim < fim_mes or inicio > inicio_mes
        percentuais = [cobertura["chuva_pct"], cobertura["temperatura_pct"]]
        resultado.append({
            "estacao": estacao.codigo, "nome": estacao.nome,
            "periodo": {"inicio": _iso(inicio), "fim": _iso(fim)},
            "resultado": "AMOSTRA" if amostra else "MENSAL",
            **cobertura,
            "atinge_meta": None if None in percentuais else min(percentuais) >= META_COBERTURA_PCT,
            "sem_medicao": sem_medicao[:LIMITE_SEM_MEDICAO_LISTADOS],
            "sem_medicao_truncado": len(sem_medicao) > LIMITE_SEM_MEDICAO_LISTADOS,
        })
    coletas = Counter(dict(db.session.query(ColetaClima.estado, func.count()).filter(
        ColetaClima.fonte == fonte, ColetaClima.prevista_em >= inicio_mes,
        ColetaClima.prevista_em < fim_mes).group_by(ColetaClima.estado).all()))
    return {
        "fonte": fonte, "mes": f"{ano:04d}-{mes:02d}", "fuso_do_mes": "America/Sao_Paulo",
        "meta_pct": META_COBERTURA_PCT, "denominador": DENOMINADOR,
        "estacoes_consideradas": "usadas por trechos (ASSOCIADO)" if usadas else "todas da fonte (nenhum vínculo)",
        "limitacoes": [
            "AMOSTRA: mês em curso ou estação iniciada no meio do mês; não vale como resultado mensal.",
            "Mede a chegada das observações; não valida a medição contra outra fonte.",
        ],
        "estacoes": resultado,
        "coletas_por_estado": dict(sorted(coletas.items())),
        "calculado_em": _iso(agora),
    }


def consultar_mapa_clima(instante, fonte="INMET"):
    """Clima no mapa ao vivo (US06 + US22): cada estação da fonte com a observação da última
    janela nativa inteira terminada até ``instante`` (INMET 1 h, CGE 15 min).

    FeatureCollection GeoJSON com um ponto por estação. Estação sem observação nessa janela
    aparece com ``chuva_mm``/``temperatura_c`` null e motivo — nunca chuva zero.
    """
    intervalo = INTERVALO_FONTE[fonte]
    fim = datetime.fromtimestamp(int(instante.astimezone(UTC).timestamp()) // intervalo * intervalo, UTC)
    estacoes = (EstacaoMeteorologica.query.filter_by(fonte=fonte)
                .order_by(EstacaoMeteorologica.codigo, EstacaoMeteorologica.id).all())
    observacoes = {}
    if estacoes:
        for o in ObservacaoClima.query.filter(ObservacaoClima.estacao_id.in_([e.id for e in estacoes]),
                                              ObservacaoClima.instante == fim):
            observacoes[o.estacao_id] = o
    features = []
    for estacao in estacoes:
        o = observacoes.get(estacao.id)
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [float(estacao.longitude), float(estacao.latitude)]},
            "properties": {
                "estacao_id": estacao.id, "codigo": estacao.codigo, "nome": estacao.nome, "fonte": fonte,
                "chuva_mm": _numero(o.chuva) if o else None,
                "temperatura_c": _numero(o.temperatura) if o else None,
                "motivo": None if o else "sem_observacao",
            },
        })
    return {
        "type": "FeatureCollection", "features": features, "fonte": fonte,
        "janela": {"inicio": _iso(fim - timedelta(seconds=intervalo)), "fim": _iso(fim), "intervalo_seg": intervalo},
        "unidades": UNIDADES,
        "convencao": ("chuva_mm é o total acumulado na janela (inicio, fim]; temperatura_c é a leitura "
                      "no fim da janela. null significa sem medição, nunca ausência de chuva."),
        "motivo": None if estacoes else "sem_estacoes",
    }
