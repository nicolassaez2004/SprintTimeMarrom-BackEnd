"""US30-T02: disponibilidade mensal por serviço, com lacunas de monitoramento explícitas.

Período: mês civil de ``America/Sao_Paulo`` → ``[início, fim)`` em UTC; em mês
corrente, o corte é o último minuto completo (``mes_parcial = true``).

API e PAINEL (sondagens ``VerificacaoSaude``), por minuto UTC:

- minuto com sondagem: sucesso só se todas as sondagens dele foram OK;
- minuto sem sondagem: **lacuna de monitoramento** — não é sucesso nem queda.

    percentual_observado = sucessos / minutos_observados
    limite_inferior      = sucessos / minutos_esperados            (lacunas = queda)
    limite_superior      = (sucessos + lacunas) / minutos_esperados (lacunas = sucesso)

Meta de 99% (``>=``; exatamente 99% atende): ``meta_atendida`` é ``true`` só em
mês encerrado com limite inferior ≥ 99, ``false`` só com limite superior < 99;
nos demais casos é ``null`` com motivo (mês parcial ou lacunas que impedem
concluir). Falha da fonte (SPTrans) não existe nestas séries: uma resposta HTTP
ruim da API é indisponibilidade observada, qualquer que seja a causa.

Coleta: ``sucesso_coleta.taxa_coleta``, a única série que exclui falhas da fonte.
"""
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from flask import current_app

from app.core.tempo import FUSO
from app.models import VerificacaoSaude
from app.monitoramento.sucesso_coleta import taxa_coleta

META = Decimal(99)
MINUTO = timedelta(minutes=1)
LIMITE_LISTAS = 50


def limites_mes(valor):
    if not isinstance(valor, str) or not re.fullmatch(r"\d{4}-\d{2}", valor):
        raise ValueError("mês deve estar no formato AAAA-MM")
    ano, mes = map(int, valor.split("-"))
    if not 1 <= mes <= 12 or ano < 1:
        raise ValueError("mês inválido")
    inicio = datetime(ano, mes, 1, tzinfo=FUSO)
    seguinte = datetime(ano + (mes == 12), mes % 12 + 1, 1, tzinfo=FUSO)
    return inicio.astimezone(timezone.utc), seguinte.astimezone(timezone.utc)


def _pct(n, d):
    return float((Decimal(n) * 100 / d).quantize(Decimal("0.0001"))) if d else None


def _intervalos(minutos):
    """Minutos ordenados → [(início, fim_exclusivo, quantidade)] contíguos."""
    blocos = []
    for minuto in minutos:
        if blocos and blocos[-1][1] == minuto:
            blocos[-1] = (blocos[-1][0], minuto + MINUTO, blocos[-1][2] + 1)
        else:
            blocos.append((minuto, minuto + MINUTO, 1))
    return blocos


def _lista(blocos, **extra):
    itens = [{"inicio": a.isoformat(), "fim_exclusivo": b.isoformat(), "minutos": n, **extra}
             for a, b, n in reversed(blocos)][:LIMITE_LISTAS]
    return {"total": len(blocos), "truncado": len(blocos) > LIMITE_LISTAS, "itens": itens}


def _servico(minutos, inicio, corte, parcial):
    esperados = int((corte - inicio) / MINUTO) if corte > inicio else 0
    observados = len(minutos)
    sucessos = sum(1 for ok in minutos.values() if ok)
    lacunas = esperados - observados
    inferior, superior = _pct(sucessos, esperados), _pct(sucessos + lacunas, esperados)
    if esperados == 0:
        meta, motivo = None, "periodo_sem_minutos_esperados"
    elif parcial:
        meta, motivo = None, "mes_parcial"
    elif Decimal(sucessos) * 100 >= META * esperados:
        meta, motivo = True, None
    elif Decimal(sucessos + lacunas) * 100 < META * esperados:
        meta, motivo = False, None
    else:
        meta, motivo = None, "lacunas_de_monitoramento_impedem_concluir"
    todos = [inicio + i * MINUTO for i in range(esperados)]
    falhas = _intervalos([m for m in sorted(minutos) if not minutos[m]])
    sem_medicao = _intervalos([m for m in todos if m not in minutos])
    return {
        "minutos_esperados": esperados, "minutos_observados": observados,
        "minutos_sucesso": sucessos, "minutos_falha": observados - sucessos,
        "minutos_sem_medicao": lacunas,
        "numerador": sucessos, "denominador_observado": observados,
        "percentual_observado": _pct(sucessos, observados),
        "percentual_cobertura": _pct(observados, esperados),
        "limite_inferior": inferior, "limite_superior": superior,
        "meta_percentual": float(META), "meta_atendida": meta, "motivo_meta_indefinida": motivo,
        "incidentes": _lista(falhas, tipo="INDISPONIBILIDADE_OBSERVADA"),
        "lacunas_monitoramento": _lista(sem_medicao, tipo="SEM_MEDICAO"),
    }


def disponibilidade_mensal(mes, *, agora=None, intervalo_coleta_seg=None):
    inicio, fim = limites_mes(mes)
    agora = agora or datetime.now(timezone.utc)
    if agora.tzinfo is None or agora.utcoffset() is None:
        raise ValueError("agora deve ter fuso horário")
    agora = agora.astimezone(timezone.utc)
    if agora < inicio:
        raise ValueError("mês futuro")
    corte = min(fim, agora.replace(second=0, microsecond=0))  # só minutos completos
    parcial = corte < fim
    V = VerificacaoSaude
    minutos = {c: {} for c in ("API", "PAINEL")}
    registros = {c: 0 for c in minutos}
    for componente, instante, estado in (
            V.query.with_entities(V.componente, V.verificada_em, V.estado)
            .filter(V.verificada_em >= inicio, V.verificada_em < corte)):
        if componente in minutos:
            minuto = instante.astimezone(timezone.utc).replace(second=0, microsecond=0)
            minutos[componente][minuto] = minutos[componente].get(minuto, True) and estado == "OK"
            registros[componente] += 1
    servicos = {}
    for componente, grupo in minutos.items():
        servicos[componente] = _servico(grupo, inicio, corte, parcial)
        servicos[componente]["sondagens_registradas"] = registros[componente]
        servicos[componente]["sondagens_adicionais_no_mesmo_minuto"] = registros[componente] - len(grupo)
    intervalo = intervalo_coleta_seg or current_app.config["GPS_COLLECTION_INTERVAL_SECONDS"]
    coleta = taxa_coleta(inicio, corte, intervalo)
    coleta["meta_percentual"] = float(META)
    if coleta["percentual"] is None:
        coleta["meta_atendida"], coleta["motivo_meta_indefinida"] = None, coleta["motivo_sem_percentual"]
    elif parcial:
        coleta["meta_atendida"], coleta["motivo_meta_indefinida"] = None, "mes_parcial"
    elif Decimal(coleta["numerador"]) * 100 >= META * coleta["denominador"]:
        coleta["meta_atendida"], coleta["motivo_meta_indefinida"] = True, None
    elif (coleta["denominador"] - coleta["indeterminados_total"] > 0 and Decimal(coleta["numerador"]) * 100
          < META * (coleta["denominador"] - coleta["indeterminados_total"])):
        coleta["meta_atendida"], coleta["motivo_meta_indefinida"] = False, None
    else:
        coleta["meta_atendida"], coleta["motivo_meta_indefinida"] = None, "ciclos_indeterminados_impedem_concluir"
    return {
        "mes": mes, "fuso_mes": "America/Sao_Paulo", "inicio_utc": inicio.isoformat(),
        "fim_mes_utc": fim.isoformat(), "corte_exclusivo_utc": corte.isoformat(),
        "mes_parcial": parcial,
        "servicos": {"API": servicos["API"], "PAINEL": servicos["PAINEL"], "COLETA": coleta},
        "politica_lacunas": "Minuto sem sondagem não é sucesso nem queda; a meta só é concluída "
                            "quando os limites inferior e superior concordam.",
    }
