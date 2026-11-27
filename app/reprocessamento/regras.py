"""Localiza indicadores publicados com uma versão de regra em um intervalo.

Identifica uso direto de ``versao_calculo`` e dependências confirmadas.
"""

from datetime import timezone

from app.models import IndicadorOperacional


def localizar_uso_direto(versao_regra, inicio, fim):
    """Retorna registros existentes que intersectam [inicio, fim), sem alterá-los.

    O período retornado é o período integral de cada resultado: um cálculo que
    intersecta parcialmente a alteração precisa ser refeito na janela inteira.
    """
    if not isinstance(versao_regra, str) or not versao_regra.strip():
        raise ValueError("informe a versão da regra")
    if inicio.tzinfo is None or inicio.utcoffset() is None or fim.tzinfo is None or fim.utcoffset() is None:
        raise ValueError("início e fim devem conter fuso horário")
    inicio, fim = inicio.astimezone(timezone.utc), fim.astimezone(timezone.utc)
    if inicio >= fim:
        raise ValueError("o fim deve ser posterior ao início")
    return (IndicadorOperacional.query.filter(
        IndicadorOperacional.versao_calculo == versao_regra,
        IndicadorOperacional.periodo_inicio < fim,
        IndicadorOperacional.periodo_fim > inicio,
    ).order_by(IndicadorOperacional.periodo_inicio, IndicadorOperacional.id).all())


def localizar_regra_e_dependentes(versao_regra, inicio, fim):
    """Localiza usos diretos e dependentes publicados no mesmo escopo e janela.

    Não infere dependências de regras de entrada que não estejam registradas em
    ``versao_calculo``; o grafo contém apenas relações confirmadas no código.
    """
    from app.reprocessamento.dependencias import incluir_dependentes

    diretos = localizar_uso_direto(versao_regra, inicio, fim)
    encontrados = {item.id: item for item in diretos}
    for item in diretos:
        dependentes = incluir_dependentes({item.indicador}) - {item.indicador}
        if not dependentes:
            continue
        consulta = IndicadorOperacional.query.filter(
            IndicadorOperacional.indicador.in_(dependentes),
            IndicadorOperacional.versao_gtfs_id == item.versao_gtfs_id,
            IndicadorOperacional.linha_codigo == item.linha_codigo,
            IndicadorOperacional.sentido == item.sentido,
            IndicadorOperacional.parada_codigo == item.parada_codigo,
            IndicadorOperacional.trecho_id == item.trecho_id,
            IndicadorOperacional.periodo_inicio < item.periodo_fim,
            IndicadorOperacional.periodo_fim > item.periodo_inicio,
            IndicadorOperacional.periodo_inicio < fim,
            IndicadorOperacional.periodo_fim > inicio,
        )
        encontrados.update((resultado.id, resultado) for resultado in consulta)
    return sorted(encontrados.values(), key=lambda item: (item.periodo_inicio, item.id))
