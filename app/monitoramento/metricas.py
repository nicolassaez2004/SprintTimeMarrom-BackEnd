"""Gravação confirmada em até 60 s, com janela e denominador explícitos."""

from decimal import Decimal
from math import ceil

from sqlalchemy import func

from app.models import ExecucaoColeta


def medir_gravacao(inicio, fim, intervalo=30):
    if inicio.tzinfo is None or fim.tzinfo is None or fim <= inicio:
        raise ValueError("Informe uma janela crescente com fuso horário")
    if not 15 <= intervalo <= 60:
        raise ValueError("Intervalo precisa estar entre 15 e 60 segundos")
    instante = func.coalesce(ExecucaoColeta.prevista_em, ExecucaoColeta.iniciada_em)
    ciclos = ExecucaoColeta.query.filter(
        ExecucaoColeta.fonte == "OLHO_VIVO", instante >= inicio, instante < fim
    ).all()
    esperados = ceil((fim - inicio).total_seconds() / intervalo)
    rapidos = sum(
        e.estado == "OK"
        and e.confirmada_em is not None
        and e.duracao_gravacao_ms is not None
        and 0 <= e.duracao_gravacao_ms <= 60000
        for e in ciclos
    )
    desconhecidos = sum(
        e.estado == "OK" and (e.confirmada_em is None or e.duracao_gravacao_ms is None)
        for e in ciclos
    )
    denominador = max(esperados, len(ciclos))
    taxa = Decimal(rapidos) * 100 / Decimal(denominador)
    return dict(
        inicio=inicio.isoformat(),
        fim_exclusivo=fim.isoformat(),
        intervalo_segundos=intervalo,
        ciclos_observados=len(ciclos),
        ciclos_esperados=esperados,
        sem_ciclo_registrado=max(0, esperados - len(ciclos)),
        confirmados_ate_60s=rapidos,
        confirmacao_desconhecida=desconhecidos,
        falhas=sum(e.estado == "FALHA" for e in ciclos),
        denominador=denominador,
        percentual_ate_60s=float(taxa.quantize(Decimal(".01"))),
        meta_percentual=99,
        meta_atingida=taxa >= 99,
        observacao_mensal_comprovada=False,
        nota="Janela informada; confirmação ausente e ciclos não registrados não contam como rápidos. Uma janela curta não comprova observação mensal.",
    )
