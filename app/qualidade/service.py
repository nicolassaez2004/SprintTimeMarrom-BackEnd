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

`FalhasColeta` (US26-T01) soma essas janelas de 15 min para a janela exibida
num painel e grava o resultado em `IndicadorOperacional.qualidade`.
"""

from bisect import bisect_left
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from math import ceil

from flask import current_app
from sqlalchemy import func, or_

from app import db
from app.core.tempo import FUSO
from app.config import validar_intervalo_coleta
from app.gtfs.service import VersaoGTFSAmbigua, VersaoGTFSInexistente, versao_vigente
from app.models import (
    ExecucaoColeta, LacunaColeta, ObservacaoTratada, RegistroBrutoGPS, agora_utc,
)
from app.monitoramento.metricas import falha_da_fonte

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


def _falhas(c):
    """(falhas, denominador) de uma janela: ciclos esperados sem dados."""
    denominador = max(c["ciclos_esperados"], c["ciclos_registrados"])
    return max(denominador - c["ciclos_com_dados"], 0), denominador


def _percentual(c):
    falhas, denominador = _falhas(c)
    if denominador == 0:
        return None
    return (Decimal(falhas) * 100 / denominador).quantize(Decimal("0.01"))


def _resumir(c, situacao=None):
    """Completa os contadores com percentual, marca de incompleto e situação."""
    pct = _percentual(c)
    falhas, denominador = _falhas(c)
    # Mesma regra exata da US26 (classificar_falhas): o arredondamento do
    # percentual exibido (10,004% → 10,00%) não pode esconder o aviso.
    acima = denominador > 0 and falhas * 100 > LIMITE_INCOMPLETO_PCT * denominador
    if situacao is None:
        if c["ciclos_com_dados"] == 0:
            situacao = "SEM_DADOS"
        elif acima:
            situacao = "INCOMPLETA"
        else:
            situacao = "COMPLETA" if falhas == 0 else "COM_FALHAS"
    return {**c, "situacao": situacao,
            "percentual_falhas": float(pct) if pct is not None else None,
            "incompleto": None if pct is None else acima}


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


def _lacuna_no_dia(lacuna, inicio_dia, fim_dia, observado_ate):
    """Recorte diário sem perder início/fim reais nem encerrar lacuna aberta."""
    fim_observado = min(lacuna["fim"] or observado_ate, fim_dia, observado_ate)
    return {
        **_lacuna_publica(lacuna),
        "inicio_no_dia": _iso(max(lacuna["inicio"], inicio_dia)),
        "fim_no_dia": _iso(fim_observado),
        "continua_do_dia_anterior": lacuna["inicio"] < inicio_dia,
        "continua_no_dia_seguinte": (lacuna["fim"] or observado_ate) > fim_dia,
    }


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
            "lacunas": [_lacuna_no_dia(l, d0, d1, observado_ate) for l in lacunas
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


# --- US26-T01: percentual de falhas da janela exibida -------------------------------

VERSAO_REGRA_FALHAS = "us26-falhas-coleta-1.0"
# ExecucaoColeta não tem linha: cada ciclo consulta a frota inteira.
ESCOPO_FALHAS = "COLETA_GLOBAL"
SEM_EXPECTATIVA = ("PENDENTE", "FORA_DO_MONITORAMENTO")


def classificar_falhas(falhas, denominador, com_dados, motivo_sem_expectativa):
    """(situacao, motivo) da regra US26: estritamente acima de 10% alerta; sem
    ciclos esperados não há medida (nunca vira coleta completa)."""
    if denominador == 0:
        return "SEM_EXPECTATIVA", motivo_sem_expectativa
    if com_dados == 0:
        return "SEM_DADOS", "NENHUM_CICLO_COM_DADOS"
    if falhas * 100 > LIMITE_INCOMPLETO_PCT * denominador:
        return "INCOMPLETA", "FALHAS_ACIMA_DO_LIMITE"
    return ("COMPLETA" if falhas == 0 else "COM_FALHAS"), None


def _alinhado(instante):
    local = instante.astimezone(FUSO)
    return (local.minute * 60 + local.second) % JANELA.total_seconds() == 0 and not local.microsecond


class FalhasColeta:
    """Percentual de falhas de coleta de janelas [inicio, fim) alinhadas a 15 min.

    Reaproveita as janelas da US13 (uma consulta por dia, em cache) e soma
    as contagens; percentual = falhas ÷ denominador × 100, com o denominador
    da US13 (max(esperados, registrados)) em cada janela de 15 min. Ciclos
    são da frota inteira: o resultado vale para qualquer linha, sem ser
    específico dela. Somente leitura.
    """

    def __init__(self, *, agora=None, intervalo=None):
        self.agora = agora or agora_utc()
        self.intervalo = intervalo
        self._dias = {}

    def _dia(self, dia):
        if dia not in self._dias:
            relatorio = consolidar_qualidade(dia, dia, agora=self.agora, intervalo=self.intervalo,
                                             incluir_janelas=True)
            self.intervalo = relatorio["intervalo_coleta_segundos"]
            self._dias[dia] = relatorio["dias"][0]
        return self._dias[dia]

    def da_janela(self, inicio, fim, *, linha=None, versao_gtfs_id=None):
        if inicio.tzinfo is None or fim.tzinfo is None:
            raise ValueError("inicio e fim devem conter fuso horário")
        if fim <= inicio:
            raise ValueError("fim deve ser posterior ao início")
        janelas, lacunas = [], {}
        if _alinhado(inicio) and _alinhado(fim):
            dia, ultimo = inicio.astimezone(FUSO).date(), (fim - timedelta(microseconds=1)).astimezone(FUSO).date()
            while dia <= ultimo:
                registro = self._dia(dia)
                janelas += [j for j in registro["janelas"]
                            if inicio <= datetime.fromisoformat(j["inicio"])
                            and datetime.fromisoformat(j["fim"]) <= fim]
                for l in registro["lacunas"]:
                    if (datetime.fromisoformat(l["inicio"]) < fim
                            and (l["fim"] is None or datetime.fromisoformat(l["fim"]) > inicio)):
                        lacunas[l["id"]] = {k: l[k] for k in ("id", "origem", "causa", "inicio", "fim", "estado")}
                dia += timedelta(days=1)
        medidas = [j for j in janelas if j["situacao"] not in SEM_EXPECTATIVA]
        contagens = _somar(medidas) if medidas else dict.fromkeys(_CONTADORES, 0)
        pares = [_falhas(j) for j in medidas]  # soma por janela: sem compensar entre janelas
        falhas, denominador = sum(f for f, _ in pares), sum(d for _, d in pares)

        if not janelas:
            sem_expectativa = "JANELA_FORA_DA_GRADE_15MIN"
        elif not medidas:
            pendentes = all(j["situacao"] == "PENDENTE" for j in janelas)
            sem_expectativa = "JANELA_FUTURA" if pendentes else "FORA_DO_MONITORAMENTO"
        else:
            sem_expectativa = "DENOMINADOR_ZERO"
        situacao, motivo = classificar_falhas(falhas, denominador, contagens["ciclos_com_dados"],
                                              sem_expectativa)
        medido = situacao != "SEM_EXPECTATIVA"
        pct = (Decimal(falhas) * 100 / denominador).quantize(Decimal("0.01")) if medido else None
        return {
            "versao_regra": VERSAO_REGRA_FALHAS, "escopo": ESCOPO_FALHAS, "fonte": FONTE,
            "linha_exibida": linha, "inicio": _iso(inicio), "fim": _iso(fim),
            "intervalo_coleta_segundos": self.intervalo,
            "janelas_15min": len(janelas), "janelas_sem_expectativa": len(janelas) - len(medidas),
            **contagens, "falhas": falhas, "denominador": denominador,
            "percentual_falhas": float(pct) if medido else None,
            "limite_percentual": float(LIMITE_INCOMPLETO_PCT),
            # Critério oficial: estritamente acima de 10%; exatamente 10% não alerta.
            "incompleto": falhas * 100 > LIMITE_INCOMPLETO_PCT * denominador if medido else None,
            "situacao": situacao, "motivo": motivo,
            "lacunas": sorted(lacunas.values(), key=lambda l: (l["inicio"], l["id"])),
            "operacao_linha": self._operacao(linha, versao_gtfs_id, inicio, fim)
            if linha and contagens["ciclos_com_dados"] else None,
        }

    def _operacao(self, linha, versao_gtfs_id, inicio, fim):
        """Coleta válida sem posições da linha é ausência operacional, não falha."""
        fim = min(fim, self.agora)
        R, O = RegistroBrutoGPS, ObservacaoTratada
        na_janela = (R.coletado_em >= inicio, R.coletado_em < fim)
        da_linha = db.session.query(R.id).join(O, O.bruto_id == R.id).filter(
            *na_janela, O.linha_codigo == linha,
            *([O.versao_gtfs_id == versao_gtfs_id] if versao_gtfs_id else []))
        if db.session.query(da_linha.exists()).scalar():
            return "COM_POSICOES"
        # Bruto sem tratamento: a US03 ainda não disse a que linha pertence.
        pendente = db.session.query(R.id).outerjoin(O, O.bruto_id == R.id).filter(
            *na_janela, O.id.is_(None))
        if db.session.query(pendente.exists()).scalar():
            return "CORRESPONDENCIA_PENDENTE"
        return "AUSENCIA_OPERACIONAL"


def com_falhas_coleta(campos, falhas):
    """`qualidade` de um indicador acrescida do bloco US26 da sua janela."""
    return {**(campos.get("qualidade") or {}), "falhas_coleta": falhas.da_janela(
        campos["periodo_inicio"], campos["periodo_fim"],
        linha=campos.get("linha_codigo"), versao_gtfs_id=campos.get("versao_gtfs_id"))}
