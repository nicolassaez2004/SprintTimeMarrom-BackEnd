"""Consulta paginada dos valores operacionais publicados."""

from app import db
from app.models import IndicadorOperacional


def consultar_indicadores(filtros, *, serie=False):
    consulta = db.session.query(IndicadorOperacional)
    if filtros["linha"] is not None:
        consulta = consulta.filter(IndicadorOperacional.linha_codigo == filtros["linha"])
    if filtros["indicador"] is not None:
        consulta = consulta.filter(IndicadorOperacional.indicador == filtros["indicador"])
    if filtros["inicio"] is not None:
        consulta = consulta.filter(IndicadorOperacional.periodo_fim > filtros["inicio"])
    if filtros["fim"] is not None:
        consulta = consulta.filter(IndicadorOperacional.periodo_inicio <= filtros["fim"])

    total = consulta.count()
    registros = (
        consulta.order_by(
            IndicadorOperacional.periodo_inicio.asc(),
            IndicadorOperacional.linha_codigo.asc(),
            IndicadorOperacional.indicador.asc(),
            IndicadorOperacional.id.asc(),
        )
        .offset((filtros["page"] - 1) * filtros["page_size"])
        .limit(filtros["page_size"])
        .all()
    )
    dados = []
    for registro in registros:
        valor = {
            "linha": registro.linha_codigo,
            "indicador": registro.indicador,
            "valor": registro.valor,
            "unidade": registro.unidade,
            "versao_conjunto": registro.versao_conjunto,
            "qualidade": registro.qualidade or {},
        }
        if serie:
            valor["instante"] = registro.periodo_inicio.isoformat()
        else:
            valor["inicio"] = registro.periodo_inicio.isoformat()
            valor["fim"] = registro.periodo_fim.isoformat()
        dados.append(valor)

    return {
        "dados": dados,
        "paginacao": {
            "page": filtros["page"],
            "page_size": filtros["page_size"],
            "total": total,
        },
    }
