"""Consolidação da qualidade da coleta por dia e janela de 15 min (US13-T01).

Regras (somente leitura; nada é gravado nem preenchido nos brutos):

- Dia operacional de São Paulo; janelas de 15 min a partir da meia-noite local.
- Ciclo pertence à janela do seu instante previsto (`prevista_em`, ou
  `iniciada_em` em ciclos antigos), como em `app.monitoramento.metricas`.
- Ciclos esperados = ceil(duração observada / intervalo configurado). Só conta
  o tempo já decorrido e posterior ao primeiro ciclo registrado: antes disso a
  plataforma não monitorava, e o futuro ainda não aconteceu.
- Percentual de falhas = ciclos esperados sem dados (falha, interrompido ou
  sem registro) ÷ max(esperados, registrados). Janela sem dados é
  `SEM_DADOS`, nunca "zero ônibus"; acima de 10% é marcada `incompleto` (US26).
- Lacunas: as registradas pelo coletor (`LacunaColeta`, US29) e, nos
  intervalos que elas não cobrem, silêncios sem nenhum ciclo registrado
  (processo parado), inferidos com causa `SEM_CICLO_REGISTRADO`.
- Versão GTFS de cada dia: `versao_vigente` (US02-T03), sem escolher em
  silêncio quando não há versão ou há vigências sobrepostas.
"""

from bisect import bisect_left
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from math import ceil
from zoneinfo import ZoneInfo

from flask import current_app
from sqlalchemy import func, or_

from app import db
from app.config import validar_intervalo_coleta
from app.gtfs.service import VersaoGTFSAmbigua, VersaoGTFSInexistente, versao_vigente
from app.models import ExecucaoColeta, LacunaColeta, agora_utc
from app.monitoramento.metricas import falha_da_fonte

FUSO = ZoneInfo("America/Sao_Paulo")
FONTE = "OLHO_VIVO"
JANELA = timedelta(minutes=15)
LIMITE_INCOMPLETO_PCT = Decimal("10")
CAUSA_SEM_CICLO = "SEM_CICLO_REGISTRADO"
# Intervalo entre ciclos acima de 1,5 × o configurado = ao menos um ciclo ausente.
TOLERANCIA_SILENCIO = Decimal("1.5")

_CONTADORES = ("ciclos_esperados", "ciclos_registrados", "ciclos_com_dados",
               "ciclos_falha", "ciclos_em_andamento", "falhas_da_fonte", "sem_registro")


def _meia_noite(dia):
    return datetime.combine(dia, time(), FUSO)


def _iso(valor):
    """Instantes saem em UTC, como gravados; datas, como estão."""
    if isinstance(valor, datetime):
        valor = valor.astimezone(timezone.utc)
    return valor.isoformat() if valor else None


def _percentual(c):
    denominador = max(c["ciclos_esperados"], c["ciclos_registrados"])
    if denominador == 0:
        return None
    falhas = max(denominador - c["ciclos_com_dados"], 0)
    return (Decimal(falhas) * 100 / denominador).quantize(Decimal("0.01"))


def _resumir(c, situacao=None):
    """Completa os contadores com percentual, marca de incompleto e situação."""
    pct = _percentual(c)
    if situacao is None:
        if c["ciclos_com_dados"] == 0:
            situacao = "SEM_DADOS"
        elif pct > LIMITE_INCOMPLETO_PCT:
            situacao = "INCOMPLETA"
        else:
            situacao = "COMPLETA" if pct == 0 else "COM_FALHAS"
    return {**c, "situacao": situacao,
            "percentual_falhas": float(pct) if pct is not None else None,
            "incompleto": None if pct is None else pct > LIMITE_INCOMPLETO_PCT}


def _contar(ciclos, inicio, fim, monitoramento, agora, intervalo):
    """Contadores de [inicio, fim); `ciclos` = (instantes ordenados, linhas)."""
    instantes, linhas = ciclos
    if inicio >= agora:
        return None, "PENDENTE"
    if monitoramento is None or fim <= monitoramento:
        return None, "FORA_DO_MONITORAMENTO"
    lo, hi = max(inicio, monitoramento), min(fim, agora)
    na_janela = linhas[bisect_left(instantes, inicio):bisect_left(instantes, fim)]
    esperados = ceil((hi - lo).total_seconds() / intervalo)
    c = {
        "ciclos_esperados": esperados,
        "ciclos_registrados": len(na_janela),
        "ciclos_com_dados": sum(e.estado in ("OK", "PARCIAL") for e in na_janela),
        "ciclos_falha": sum(e.estado == "FALHA" for e in na_janela),
        "ciclos_em_andamento": sum(e.estado == "EM_ANDAMENTO" for e in na_janela),
        "falhas_da_fonte": sum(falha_da_fonte(e) for e in na_janela),
    }
    c["sem_registro"] = max(esperados - len(na_janela), 0)
    return c, None


def _somar(partes):
    return {k: sum(p[k] for p in partes) for k in _CONTADORES}


def _subtrair(trecho, cobertos):
    """Partes de `trecho` (início, fim) não cobertas pelos intervalos dados."""
    partes = [trecho]
    for c0, c1 in cobertos:
        restantes = []
        for p0, p1 in partes:
            if c1 <= p0 or c0 >= p1:
                restantes.append((p0, p1))
                continue
            if p0 < c0:
                restantes.append((p0, c0))
            if c1 < p1:
                restantes.append((c1, p1))
        partes = restantes
    return partes


def _silencios(instantes, anterior, posterior, agora, intervalo, cobertos):
    """Períodos sem ciclo registrado; fim None = silêncio ainda em curso."""
    pontos = ([anterior] if anterior else []) + instantes
    limite = timedelta(seconds=float(TOLERANCIA_SILENCIO * intervalo))
    lacunas = []
    for a, b in zip(pontos, pontos[1:] + [posterior]):
        ate = b or agora
        if ate - a <= limite:
            continue
        for p0, p1 in _subtrair((a + timedelta(seconds=intervalo), ate), cobertos):
            aberta = b is None and p1 == agora
            lacunas.append({
                "id": f"inferida:{p0.isoformat()}", "origem": "INFERIDA",
                "causa": CAUSA_SEM_CICLO, "inicio": p0, "fim": None if aberta else p1,
            })
    return lacunas


def _gtfs_do_dia(dia):
    def resumo(v):
        return {"id": v.id, "hash": v.hash, "capturada_em": _iso(v.capturada_em),
                "vigencia_inicio": _iso(v.vigencia_inicio), "vigencia_fim": _iso(v.vigencia_fim)}
    try:
        return {"situacao": "VIGENTE", "versao": resumo(versao_vigente(dia)), "candidatas": []}
    except VersaoGTFSInexistente:
        return {"situacao": "SEM_VERSAO_VIGENTE", "versao": None, "candidatas": []}
    except VersaoGTFSAmbigua as erro:
        return {"situacao": "AMBIGUA", "versao": None,
                "candidatas": [resumo(v) for v in erro.versoes]}


def _lacuna_publica(lacuna):
    return {**lacuna, "inicio": _iso(lacuna["inicio"]), "fim": _iso(lacuna["fim"]),
            "estado": "ABERTA" if lacuna["fim"] is None else "ENCERRADA"}


def consolidar_qualidade(inicio, fim, *, agora=None, intervalo=None, incluir_janelas=False):
    """Qualidade da coleta de cada dia em [inicio, fim] (datas de São Paulo)."""
    for valor in (inicio, fim):
        if isinstance(valor, datetime) or not isinstance(valor, date):
            raise TypeError("inicio e fim devem ser datetime.date")
    if fim < inicio:
        raise ValueError("fim deve ser igual ou posterior ao início")
    agora = agora or agora_utc()
    if agora.tzinfo is None:
        raise ValueError("agora deve conter fuso horário")
    intervalo = validar_intervalo_coleta(
        intervalo or current_app.config["GPS_COLLECTION_INTERVAL_SECONDS"])
    periodo_ini = _meia_noite(inicio)
    periodo_fim = _meia_noite(fim + timedelta(days=1))
    observado_ate = min(periodo_fim, agora)

    E = ExecucaoColeta
    instante = func.coalesce(E.prevista_em, E.iniciada_em)
    da_fonte = E.fonte == FONTE
    monitoramento = db.session.query(func.min(instante)).filter(da_fonte).scalar()
    linhas = (db.session.query(instante.label("instante"), E.estado, E.erro)
              .filter(da_fonte, instante >= periodo_ini, instante < observado_ate)
              .order_by(instante).all())
    instantes = [linha.instante for linha in linhas]
    anterior = db.session.query(func.max(instante)).filter(da_fonte, instante < periodo_ini).scalar()
    posterior = (db.session.query(func.min(instante))
                 .filter(da_fonte, instante >= observado_ate, instante <= agora).scalar())

    L = LacunaColeta
    registradas = (L.query.filter(L.fonte == FONTE, L.inicio < observado_ate,
                                  or_(L.fim.is_(None), L.fim > periodo_ini))
                   .order_by(L.inicio, L.id).all())
    lacunas = [{"id": l.id, "origem": "REGISTRADA", "causa": l.causa,
                "inicio": l.inicio, "fim": l.fim} for l in registradas]
    cobertos = [(l["inicio"], l["fim"] or agora) for l in lacunas]
    lacunas += _silencios(instantes, anterior, posterior, agora, intervalo, cobertos)
    lacunas = [l for l in lacunas
               if l["inicio"] < observado_ate and (l["fim"] or agora) > periodo_ini]
    lacunas.sort(key=lambda l: (l["inicio"], l["id"]))

    ciclos = (instantes, linhas)
    dias = []
    dia = inicio
    while dia <= fim:
        d0, d1 = _meia_noite(dia), _meia_noite(dia + timedelta(days=1))
        janelas, a = [], d0
        while a < d1:
            b = min(a + JANELA, d1)
            c, situacao = _contar(ciclos, a, b, monitoramento, agora, intervalo)
            janelas.append((a, b, c, situacao))
            a = b
        medidas = [c for *_, c, _ in janelas if c is not None]
        if medidas:
            resumo = _resumir(_somar(medidas))
        else:
            situacoes = {s for *_, s in janelas}
            resumo = _resumir(dict.fromkeys(_CONTADORES, 0),
                              "PENDENTE" if situacoes == {"PENDENTE"} else "FORA_DO_MONITORAMENTO")
        item = {
            "data": dia.isoformat(), **resumo, "gtfs": _gtfs_do_dia(dia),
            "lacunas": [_lacuna_publica(l) for l in lacunas
                        if l["inicio"] < d1 and (l["fim"] or agora) > d0],
        }
        if incluir_janelas:
            item["janelas"] = [
                {"inicio": _iso(a), "fim": _iso(b),
                 **_resumir(c or dict.fromkeys(_CONTADORES, 0), s)}
                for a, b, c, s in janelas
            ]
        dias.append(item)
        dia += timedelta(days=1)

    publicas = [_lacuna_publica(l) for l in lacunas]
    medidos = [d for d in dias if d["situacao"] not in ("PENDENTE", "FORA_DO_MONITORAMENTO")]
    total = _resumir(_somar(medidos)) if medidos else _resumir(
        dict.fromkeys(_CONTADORES, 0), "FORA_DO_MONITORAMENTO")
    total.update(lacunas_abertas=sum(l["estado"] == "ABERTA" for l in publicas),
                 lacunas_encerradas=sum(l["estado"] == "ENCERRADA" for l in publicas))
    return {
        "periodo": {"inicio": inicio.isoformat(), "fim": fim.isoformat(),
                    "fuso": "America/Sao_Paulo", "observado_ate": _iso(observado_ate)},
        "gerado_em": _iso(agora),
        "fonte": FONTE,
        "intervalo_coleta_segundos": intervalo,
        "janela_minutos": int(JANELA.total_seconds() // 60),
        "limite_incompleto_percentual": float(LIMITE_INCOMPLETO_PCT),
        "inicio_monitoramento": _iso(monitoramento),
        "resumo": total,
        "lacunas": publicas,
        "dias": dias,
    }
