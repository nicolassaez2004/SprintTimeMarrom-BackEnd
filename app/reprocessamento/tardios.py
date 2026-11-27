"""Planejamento conservador para dados tardios com abrangência informada.

O intervalo deve abranger a viagem e as passagens vizinhas potencialmente
alteradas. A reconstrução automática dessa abrangência ainda não existe.
"""
from datetime import timezone

from app.models import IndicadorOperacional, ObservacaoTratada


def planejar_dado_tardio(bruto, *, inicio_impacto, fim_impacto,
                         versao_base, versao_regra, versao_codigo, fechamento_em):
    for nome, valor in (("versao_base", versao_base), ("versao_regra", versao_regra),
                        ("versao_codigo", versao_codigo)):
        if not isinstance(valor, str) or not valor.strip():
            raise ValueError(f"{nome} é obrigatória")
    for instante in (bruto.horario_gps, bruto.coletado_em, inicio_impacto, fim_impacto, fechamento_em):
        if instante is None or instante.tzinfo is None or instante.utcoffset() is None:
            raise ValueError("todos os horários devem estar disponíveis e ter fuso")
    inicio, fim = inicio_impacto.astimezone(timezone.utc), fim_impacto.astimezone(timezone.utc)
    horario = bruto.horario_gps.astimezone(timezone.utc)
    if not inicio <= horario < fim:
        raise ValueError("o intervalo de impacto deve conter o horário GPS")
    if not bruto.horario_gps < fechamento_em <= bruto.coletado_em:
        raise ValueError("o dado não chegou após o fechamento informado")
    tratada = ObservacaoTratada.query.filter_by(bruto_id=bruto.id).one_or_none()
    if (tratada is None or tratada.valida is not True or not tratada.linha_codigo
            or not tratada.versao_gtfs_id or tratada.sentido_gtfs not in ("0", "1")):
        raise ValueError("o dado precisa de correspondência válida com linha, sentido e GTFS")
    resultados = (IndicadorOperacional.query.filter(
        IndicadorOperacional.versao_gtfs_id == tratada.versao_gtfs_id,
        IndicadorOperacional.linha_codigo == tratada.linha_codigo,
        IndicadorOperacional.sentido == tratada.sentido_gtfs,
        IndicadorOperacional.periodo_inicio < fim,
        IndicadorOperacional.periodo_fim > inicio,
    ).order_by(IndicadorOperacional.periodo_inicio, IndicadorOperacional.id).all())
    return {"bruto_id": bruto.id, "linha_codigo": tratada.linha_codigo,
            "sentido": tratada.sentido_gtfs, "versao_gtfs_id": tratada.versao_gtfs_id,
            "inicio_impacto": inicio, "fim_impacto": fim,
            "fechamento_em": fechamento_em.astimezone(timezone.utc),
            "versao_base": versao_base, "versao_regra": versao_regra,
            "versao_codigo": versao_codigo, "resultados": resultados}
