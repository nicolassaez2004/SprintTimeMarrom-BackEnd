"""US23: leitura paginada de séries; nenhum recálculo de indicadores no HTTP."""

from datetime import datetime, time, timedelta, timezone

from sqlalchemy import and_, func, or_
from sqlalchemy.sql.expression import Grouping

from app import db
from app.core.tempo import FUSO
from app.indicadores.service import consulta_base, serializar
from app.models import ExecucaoColeta, IndicadorOperacional as Indicador

UTC = timezone.utc


def _iso(valor):
    return valor.isoformat() if valor is not None else None


def _ou_balanceado(condicoes):
    # Evita uma árvore OR linear acima do limite de expressão do SQLite.
    if len(condicoes) <= 50:
        return or_(*condicoes)
    meio = len(condicoes) // 2
    return or_(Grouping(_ou_balanceado(condicoes[:meio])),
               Grouping(_ou_balanceado(condicoes[meio:])))


def _janelas_horarias(inicio, fim, hora_inicio, hora_fim):
    """Faixa diária local, inclusive noturna; instantes efetivos sempre em UTC."""
    dia = inicio.astimezone(FUSO).date() - timedelta(days=1)
    ultimo = fim.astimezone(FUSO).date()
    primeira, ultima = time.fromisoformat(hora_inicio), time.fromisoformat(hora_fim)
    while dia <= ultimo:
        proximo = dia + timedelta(days=1) if ultima < primeira else dia
        a = datetime.combine(dia, primeira, FUSO).astimezone(UTC)
        b = datetime.combine(proximo, ultima, FUSO).astimezone(UTC)
        if b > inicio and a < fim:
            yield max(a, inicio), min(b, fim)
        dia += timedelta(days=1)


def consultar_series(filtros):
    base = consulta_base(filtros)
    # O painel de linha não mistura indicadores de uma parada/trecho com a linha.
    if filtros["parada"] is None and filtros["trecho"] is None:
        base = base.filter(Indicador.parada_codigo.is_(None), Indicador.trecho_id.is_(None))
    primeiro, ultimo = base.with_entities(
        func.min(Indicador.periodo_inicio), func.max(Indicador.periodo_fim),
    ).one()
    coleta_inicio, coleta_ultimo = db.session.query(
        func.min(ExecucaoColeta.iniciada_em), func.max(ExecucaoColeta.iniciada_em),
    ).one()
    limites_inicio = [i for i in (primeiro, coleta_inicio) if i is not None]
    limites_fim = [i for i in (ultimo, coleta_ultimo) if i is not None]
    inicio = filtros["inicio"] or (min(limites_inicio) if limites_inicio else None)
    fim = filtros["fim"] or (max(limites_fim) if limites_fim else None)
    if inicio is not None and fim is not None and inicio > fim:
        if filtros["fim"] is None:
            fim = inicio
        elif filtros["inicio"] is None:
            inicio = fim
    consulta = base
    if inicio is not None:
        consulta = consulta.filter(Indicador.periodo_inicio >= inicio)
    if fim is not None:
        consulta = consulta.filter(Indicador.periodo_fim <= fim, Indicador.periodo_inicio < fim)

    janelas = None
    if filtros["hora_inicio"] is not None and primeiro is not None:
        # A expansão é limitada ao histórico persistido, mesmo com filtros muito amplos.
        a, b = max(inicio, primeiro), min(fim, ultimo)
        janelas = list(_janelas_horarias(a, b, filtros["hora_inicio"], filtros["hora_fim"])) if a < b else []
        consulta = consulta.filter(_ou_balanceado([
            and_(Indicador.periodo_inicio >= x, Indicador.periodo_fim <= y)
            for x, y in janelas
        ]) if janelas else False)

    total = consulta.count()
    dimensoes = [Indicador.linha_codigo, Indicador.sentido, Indicador.indicador,
                 Indicador.unidade, Indicador.parada_codigo, Indicador.trecho_id]
    ordem = [Indicador.periodo_inicio, Indicador.periodo_fim, Indicador.id]
    anterior = func.max(Indicador.periodo_fim).over(
        partition_by=dimensoes, order_by=ordem, rows=(None, -1),
    ).label("anterior")
    ultima = func.row_number().over(
        partition_by=dimensoes, order_by=[c.desc() for c in ordem],
    ).label("ultima")
    # Funções de janela são aplicadas ANTES da paginação: a troca de página não cria lacunas.
    registros = consulta.add_columns(anterior, ultima).order_by(
        Indicador.periodo_inicio, Indicador.linha_codigo, Indicador.sentido,
        Indicador.indicador, Indicador.id,
    ).offset((filtros["page"] - 1) * filtros["page_size"]).limit(filtros["page_size"]).all()
    dados, lacunas = [], []
    lacunas_truncadas = False

    def lacuna(a, b, registro=None, motivo="SEM_PUBLICACAO"):
        nonlocal lacunas_truncadas
        if a is None or b is None or a >= b:
            return
        # Horas fora da faixa diária selecionada não são lacunas da consulta.
        partes = [(a, b)] if filtros["hora_inicio"] is None else _janelas_horarias(
            a, b, filtros["hora_inicio"], filtros["hora_fim"],
        )
        for x, y in partes:
            if len(lacunas) >= 200:
                lacunas_truncadas = True
                break
            lacunas.append({
                "linha": registro.linha_codigo if registro else filtros["linha"],
                "sentido": registro.sentido if registro else filtros["sentido"],
                "tipo": registro.indicador if registro else filtros["tipo"],
                "unidade": registro.unidade if registro else None,
                "inicio": _iso(x), "fim": _iso(y), "motivo": motivo,
            })

    for registro, fim_anterior, numero_reverso in registros:
        item = serializar(registro, serie=True)
        item["status"] = "SEM_RESULTADO" if registro.valor is None else "DISPONIVEL"
        item["id"] = registro.id
        dados.append(item)
        lacuna(fim_anterior or inicio, registro.periodo_inicio, registro)
        if registro.valor is None:
            lacuna(registro.periodo_inicio, registro.periodo_fim, registro, "RESULTADO_NULO")
        if numero_reverso == 1:
            # MAX anterior também cobre intervalos sobrepostos mais longos.
            lacuna(max(registro.periodo_fim, fim_anterior or registro.periodo_fim), fim, registro)
    if total == 0 and filtros["page"] == 1:
        lacuna(inicio, fim)
    return {
        "dados": dados,
        "lacunas": lacunas,
        "historico": {"coleta_inicio": _iso(coleta_inicio), "coleta_ultimo_ciclo": _iso(coleta_ultimo),
                      "inicio_indicadores": _iso(primeiro), "fim_indicadores": _iso(ultimo)},
        "consulta": {"inicio": _iso(inicio), "fim": _iso(fim),
                     "hora_inicio": filtros["hora_inicio"], "hora_fim": filtros["hora_fim"],
                     "fuso": str(FUSO), "lacunas_escopo": "PAGINA",
                     "lacunas_truncadas": lacunas_truncadas,
                     "criterio_periodo": "JANELAS_INTEIRAMENTE_CONTIDAS"},
        "paginacao": {"page": filtros["page"], "page_size": filtros["page_size"], "total": total},
    }
