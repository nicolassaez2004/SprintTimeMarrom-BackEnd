"""Consulta paginada dos valores operacionais publicados."""

from app import db
from app.models import IndicadorOperacional


def consulta_base(filtros):
    consulta = db.session.query(IndicadorOperacional)
    if filtros["linha"] is not None:
        consulta = consulta.filter(IndicadorOperacional.linha_codigo == filtros["linha"])
    if filtros["sentido"] is not None:
        consulta = consulta.filter(IndicadorOperacional.sentido == filtros["sentido"])
    if filtros["tipo"] is not None:
        consulta = consulta.filter(IndicadorOperacional.indicador == filtros["tipo"])
    if filtros["parada"] is not None:
        consulta = consulta.filter(IndicadorOperacional.parada_codigo == filtros["parada"])
    if filtros["trecho"] is not None:
        consulta = consulta.filter(IndicadorOperacional.trecho_id == filtros["trecho"])
    if filtros["versao_gtfs_id"] is not None:
        consulta = consulta.filter(IndicadorOperacional.versao_gtfs_id == filtros["versao_gtfs_id"])
    return consulta


def filtrar_periodo(consulta, filtros):
    """Regra de sobreposição da US11 para o período [inicio, fim)."""
    if filtros["inicio"] is not None:
        consulta = consulta.filter(IndicadorOperacional.periodo_fim > filtros["inicio"])
    if filtros["fim"] is not None:
        # Janelas são [inicio, fim): uma janela que começa em `fim` não cruza a
        # faixa. Com inicio == fim (consulta pontual) o instante é incluído.
        if filtros["inicio"] is not None and filtros["fim"] == filtros["inicio"]:
            consulta = consulta.filter(IndicadorOperacional.periodo_inicio <= filtros["fim"])
        else:
            consulta = consulta.filter(IndicadorOperacional.periodo_inicio < filtros["fim"])
    return consulta


def consultar_indicadores(filtros, *, serie=False):
    consulta = filtrar_periodo(consulta_base(filtros), filtros)
    total = consulta.count()
    # US27-T03: ordena e pagina só pelos ids (colunas de índice) e lê as linhas completas,
    # com o JSON de qualidade, apenas para a página; mesmo resultado e mesma ordem.
    ids = [i for (i,) in (
        consulta.with_entities(IndicadorOperacional.id).order_by(
            IndicadorOperacional.periodo_inicio.asc(),
            IndicadorOperacional.linha_codigo.asc(),
            IndicadorOperacional.sentido.asc(),
            IndicadorOperacional.indicador.asc(),
            IndicadorOperacional.id.asc(),
        )
        .offset((filtros["page"] - 1) * filtros["page_size"])
        .limit(filtros["page_size"])
        .all()
    )]
    completos = {r.id: r for r in IndicadorOperacional.query.filter(IndicadorOperacional.id.in_(ids))} if ids else {}
    registros = [completos[i] for i in ids]
    return {
        "dados": [serializar(registro, serie=serie) for registro in registros],
        "paginacao": {
            "page": filtros["page"],
            "page_size": filtros["page_size"],
            "total": total,
        },
    }


def serializar(registro, *, serie=False):
    valor = {
        "linha": registro.linha_codigo,
        "sentido": registro.sentido,
        "tipo": registro.indicador,
        "indicador": registro.indicador,
        "valor": registro.valor,
        "unidade": registro.unidade,
        "versao_conjunto": registro.versao_conjunto,
        "versao_gtfs_id": registro.versao_gtfs_id,
        "versao_correspondencia": registro.versao_correspondencia,
        "versao_calculo": registro.versao_calculo,
        "parada": registro.parada_codigo,
        "trecho": registro.trecho_id,
        "contagens": {
            "numerador": registro.contagem_numerador,
            "denominador": registro.contagem_denominador,
        },
        "qualidade": registro.qualidade or {},
    }
    valor["inicio"] = registro.periodo_inicio.isoformat()
    valor["fim"] = registro.periodo_fim.isoformat()
    if serie:
        valor["instante"] = valor["inicio"]
    return valor
