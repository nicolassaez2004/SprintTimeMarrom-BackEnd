"""Associa a qualidade de coleta US13/US26 aos períodos publicados."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from flask import current_app
from sqlalchemy.exc import SQLAlchemyError

from app import db
from app.core.tempo import FUSO
from app.qualidade.service import consolidar_qualidade

UTC = timezone.utc
LIMITE_DIAS_POR_CONSULTA = 31
SITUACOES_MEDIDAS = {"COMPLETA", "COM_FALHAS", "INCOMPLETA", "SEM_DADOS"}


def _data_final(inicio, fim):
    ultimo_instante = fim - timedelta(microseconds=1)
    return ultimo_instante.astimezone(FUSO).date()


def _sem_qualidade(item, motivo, situacao="NAO_DISPONIVEL"):
    return {
        "inicio": item.get("inicio"),
        "fim": item.get("fim"),
        "versao_gtfs_id": item.get("versao_gtfs_id"),
        "situacao": situacao,
        "percentual_falhas": None,
        "incompleto": None,
        "motivo": motivo,
        "motivos": [],
        "limite_incompleto_percentual": 10.0,
    }


def _instante(valor):
    if isinstance(valor, str):
        valor = datetime.fromisoformat(valor.replace("Z", "+00:00"))
    if valor.tzinfo is None:
        raise ValueError("período de indicador precisa conter fuso")
    return valor.astimezone(UTC)


def _relatorios_por_dia(itens, agora):
    periodos = []
    for item in itens:
        inicio, fim = _instante(item["inicio"]), _instante(item["fim"])
        if fim <= inicio:
            item["qualidade_coleta"] = _sem_qualidade(item, "PERIODO_INVALIDO")
        else:
            periodos.append((inicio, fim))
    if not periodos:
        return {}, None
    inicio = min(a for a, _ in periodos).astimezone(FUSO).date()
    fim = max(_data_final(a, b) for a, b in periodos)
    relatorios, limite_pct = {}, None
    parte = inicio
    while parte <= fim:
        limite_dias = min(fim, parte + timedelta(days=LIMITE_DIAS_POR_CONSULTA - 1))
        relatorio = consolidar_qualidade(parte, limite_dias, agora=agora, incluir_janelas=True)
        limite_pct = relatorio["limite_incompleto_percentual"]
        relatorios.update((date.fromisoformat(dia["data"]), dia) for dia in relatorio["dias"])
        parte = limite_dias + timedelta(days=1)
    return relatorios, limite_pct


def _causas_do_periodo(dia, inicio, fim):
    causas = set()
    for lacuna in dia["lacunas"]:
        a = _instante(lacuna["inicio_no_dia"])
        b = _instante(lacuna["fim_no_dia"]) if lacuna["fim_no_dia"] else fim
        if max(a, inicio) < min(b, fim):
            causas.add(lacuna["causa"])
    return causas


def _resumo_periodo(item, inicio, fim, relatorios, limite):
    versao_id = item.get("versao_gtfs_id")
    if not versao_id:
        return _sem_qualidade(item, "SEM_VERSAO_GTFS")

    primeiro_dia = inicio.astimezone(FUSO).date()
    ultimo_dia = _data_final(inicio, fim)
    dias = []
    dia_atual = primeiro_dia
    while dia_atual <= ultimo_dia:
        dia = relatorios.get(dia_atual)
        if dia is None:
            return _sem_qualidade(item, "QUALIDADE_DO_PERIODO_INDISPONIVEL")
        versao_dia = (dia.get("gtfs") or {}).get("versao") or {}
        if versao_dia.get("id") != versao_id:
            return _sem_qualidade(item, "VERSAO_GTFS_DIVERGENTE", "VERSAO_DIVERGENTE")
        dias.append((dia_atual, dia))
        dia_atual += timedelta(days=1)

    janelas = []
    causas = set()
    for dia_data, dia in dias:
        for janela in dia["janelas"]:
            a, b = _instante(janela["inicio"]), _instante(janela["fim"])
            if inicio <= a and b <= fim:
                janelas.append(janela)
                causas.update(_causas_do_periodo(dia, a, b))

    cobertura = sum(
        (_instante(janela["fim"]) - _instante(janela["inicio"])).total_seconds()
        for janela in janelas
    )
    if not janelas or cobertura != (fim - inicio).total_seconds():
        return _sem_qualidade(item, "PERIODO_NAO_ALINHADO_A_JANELAS_DE_15_MIN")

    situacoes = {janela["situacao"] for janela in janelas}
    nao_medidas = situacoes - SITUACOES_MEDIDAS
    if nao_medidas:
        situacao = "PENDENTE" if "PENDENTE" in nao_medidas else "FORA_DO_MONITORAMENTO"
        motivo = "PERIODO_AINDA_NAO_MEDIDO" if situacao == "PENDENTE" else "FORA_DO_MONITORAMENTO"
        return _sem_qualidade(item, motivo, situacao)

    esperados = sum(janela["ciclos_esperados"] for janela in janelas)
    registrados = sum(janela["ciclos_registrados"] for janela in janelas)
    com_dados = sum(janela["ciclos_com_dados"] for janela in janelas)
    denominador = max(esperados, registrados)
    percentual_decimal = (
        (Decimal(max(denominador - com_dados, 0)) * 100 / Decimal(denominador)).quantize(Decimal("0.01"))
        if denominador else None
    )
    percentual = float(percentual_decimal) if percentual_decimal is not None else None
    incompleto = percentual_decimal > Decimal(str(limite)) if percentual_decimal is not None else None
    if percentual is None:
        situacao = "SEM_DADOS"
    elif incompleto:
        situacao = "INCOMPLETA"
    elif percentual:
        situacao = "COM_FALHAS"
    else:
        situacao = "COMPLETA"

    motivos = sorted(causas)
    if percentual and not motivos:
        motivos = ["FALHAS_SEM_LACUNA_REGISTRADA"]
    motivo = motivos[0] if len(motivos) == 1 else ("MULTIPLAS_CAUSAS" if motivos else None)
    return {
        "inicio": item["inicio"],
        "fim": item["fim"],
        "versao_gtfs_id": versao_id,
        "situacao": situacao,
        "percentual_falhas": percentual,
        "incompleto": incompleto,
        "motivo": motivo,
        "motivos": motivos,
        "limite_incompleto_percentual": limite,
        "contagens": {
            "ciclos_esperados": esperados,
            "ciclos_registrados": registrados,
            "ciclos_com_dados": com_dados,
            "ciclos_falha": sum(janela["ciclos_falha"] for janela in janelas),
            "ciclos_em_andamento": sum(janela["ciclos_em_andamento"] for janela in janelas),
            "falhas_da_fonte": sum(janela["falhas_da_fonte"] for janela in janelas),
            "sem_registro": sum(janela["sem_registro"] for janela in janelas),
        },
    }


def adicionar_qualidade_coleta(itens, *, agora=None):
    """Anexa a qualidade da coleta sem misturar períodos ou versões GTFS.

    Cada item precisa ter ``inicio``, ``fim`` e ``versao_gtfs_id``. A métrica
    reutiliza a consolidação US13-T01/US26-T01 e agrega contagens, nunca médias
    de percentuais. Erros apenas nesta consulta complementar deixam explícita
    a qualidade indisponível sem remover a resposta operacional principal.
    """
    if not itens:
        return itens
    validos = []
    for item in itens:
        if item.get("inicio") is None or item.get("fim") is None:
            item["qualidade_coleta"] = _sem_qualidade(item, "SEM_PERIODO_ASSOCIADO")
        else:
            validos.append(item)
    if not validos:
        return itens
    agora = agora or datetime.now(UTC)
    try:
        with db.session.begin_nested():
            relatorios, limite = _relatorios_por_dia(validos, agora)
        for item in validos:
            if item.get("qualidade_coleta") is not None:
                continue
            try:
                inicio, fim = _instante(item["inicio"]), _instante(item["fim"])
                if fim <= inicio:
                    raise ValueError("período vazio")
                item["qualidade_coleta"] = _resumo_periodo(item, inicio, fim, relatorios, limite)
            except (KeyError, TypeError, ValueError):
                item["qualidade_coleta"] = _sem_qualidade(item, "PERIODO_INVALIDO")
    except SQLAlchemyError as erro:
        current_app.logger.error("Qualidade de coleta indisponível (%s)", type(erro).__name__)
        for item in validos:
            item["qualidade_coleta"] = _sem_qualidade(item, "QUALIDADE_INDISPONIVEL")
    return itens
