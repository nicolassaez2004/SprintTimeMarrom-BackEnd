"""US30: disponibilidade mensal e cobertura das sondagens independentes."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import re

from sqlalchemy import func

from app.core.tempo import FUSO
from app.models import ExecucaoColeta, VerificacaoDisponibilidade
from app.monitoramento.sucesso_coleta import classificar_ciclo


def _mes(valor):
    if not isinstance(valor, str) or not re.fullmatch(r"\d{4}-\d{2}", valor):
        raise ValueError("mês deve estar no formato AAAA-MM")
    try:
        ano, mes = map(int, valor.split("-"))
        inicio = datetime(ano, mes, 1, tzinfo=FUSO)
        seguinte = datetime(ano + (mes == 12), mes % 12 + 1, 1, tzinfo=FUSO)
    except (ValueError, OverflowError) as erro:
        raise ValueError("mês inválido") from erro
    return inicio.astimezone(timezone.utc), seguinte.astimezone(timezone.utc)


def disponibilidade_mensal(mes, *, agora=None):
    """Retorna taxas por alvo e contagem separada de falhas da fonte na coleta.

    Uma sondagem por minuto UTC; duplicatas usam o pior resultado. A taxa usa
    minutos observados e a meta mensal só é conclusiva com mês e cobertura completos.
    """
    inicio, fim = _mes(mes)
    agora = agora or datetime.now(timezone.utc)
    if agora.tzinfo is None or agora.utcoffset() is None:
        raise ValueError("agora deve ter fuso horário")
    agora = agora.astimezone(timezone.utc)
    if agora < inicio:
        raise ValueError("mês futuro")
    # Só minutos completos são comparáveis; exclui a janela em andamento.
    corte = min(fim, agora.replace(second=0, microsecond=0))
    esperados = int((corte - inicio).total_seconds() // 60)
    V = VerificacaoDisponibilidade
    medicoes = (V.query.filter(V.instante >= inicio, V.instante < corte)
                .order_by(V.instante, V.id).all())
    minutos = {"API": {}, "PAINEL": {}}
    totais = {"API": 0, "PAINEL": 0}
    for medicao in medicoes:
        if medicao.alvo not in minutos:
            continue
        minuto = medicao.instante.replace(second=0, microsecond=0)
        grupo = minutos[medicao.alvo]
        totais[medicao.alvo] += 1
        grupo[minuto] = grupo.get(minuto, True) and bool(medicao.sucesso)
    parcial = agora < fim
    servicos = {}
    for alvo, grupo in minutos.items():
        observados = len(grupo)
        sucessos = sum(grupo.values())
        falhas = observados - sucessos
        ausentes = esperados - observados
        taxa = Decimal(sucessos) * 100 / observados if observados else None
        cobertura = Decimal(observados) * 100 / esperados if esperados else None
        completa = not parcial and esperados > 0 and ausentes == 0
        servicos[alvo] = {
            "minutos_esperados": esperados, "minutos_observados": observados,
            "minutos_sucesso": sucessos, "minutos_falha": falhas,
            "minutos_sem_sondagem": ausentes, "registros": totais[alvo],
            "registros_adicionais_no_mesmo_minuto": totais[alvo] - observados,
            "numerador": sucessos, "denominador": observados,
            "percentual_observado": float(taxa.quantize(Decimal("0.01"))) if taxa is not None else None,
            "percentual_cobertura": float(cobertura.quantize(Decimal("0.01"))) if cobertura is not None else None,
            "meta_99_no_observado": taxa >= 99 if taxa is not None else None,
            "meta_mensal_comprovada": (taxa >= 99) if completa else None,
        }
    E = ExecucaoColeta
    instante = func.coalesce(E.prevista_em, E.iniciada_em)
    ciclos = E.query.filter(E.fonte == "OLHO_VIVO", instante >= inicio, instante < corte).all()
    categorias = ("sucesso", "falha_propria", "falha_fonte", "desconhecida", "parcial", "em_andamento")
    contagem = {categoria: 0 for categoria in categorias}
    for ciclo in ciclos:
        contagem[classificar_ciclo(ciclo)] += 1
    return {"mes": mes, "fuso_mes": "America/Sao_Paulo", "inicio_utc": inicio.isoformat(),
            "fim_mes_utc": fim.isoformat(), "corte_exclusivo_utc": corte.isoformat(),
            "mes_parcial": parcial, "servicos": servicos,
            "coleta": {"ciclos_registrados": len(ciclos), "contagens": contagem},
            "politica": "Falha HTTP é falha observada do acesso; sua causa não é atribuída à fonte. "
                        "Falhas da fonte são contadas separadamente nos ciclos de coleta."}
