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
    # US27-T03: a página é lida só com colunas do índice de cobertura
    # (ix_indicadores_serie_cobertura) e as linhas completas (com o JSON de qualidade) depois.
    # "anterior" (maior fim entre as janelas anteriores da mesma série) e "ultima" (última
    # janela da série no período) são calculados só para as linhas da página, com o mesmo
    # resultado das funções de janela usadas antes: MAX(...) OVER (... ROWS BETWEEN UNBOUNDED
    # PRECEDING AND 1 PRECEDING) é quadrático no MySQL e levava minutos num ano de dados.
    pagina = consulta.with_entities(*_ESTREITAS).order_by(
        Indicador.periodo_inicio, Indicador.linha_codigo, Indicador.sentido,
        Indicador.indicador, Indicador.id,
    ).offset((filtros["page"] - 1) * filtros["page_size"]).limit(filtros["page_size"]).all()
    marcas = _anteriores_e_ultimas(consulta, pagina)
    completos = ({r.id: r for r in Indicador.query.filter(Indicador.id.in_([p.id for p in pagina]))}
                 if pagina else {})
    registros = [(completos[p.id], *marcas[p.id]) for p in pagina]
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


_DIMENSOES = (Indicador.linha_codigo, Indicador.sentido, Indicador.indicador,
              Indicador.unidade, Indicador.parada_codigo, Indicador.trecho_id)
_ESTREITAS = (Indicador.id, Indicador.periodo_inicio, Indicador.periodo_fim) + _DIMENSOES


def _mesma_serie(chave):
    """Igualdade que trata NULL como valor (parada/trecho/unidade ausentes)."""
    return and_(*(coluna.is_not_distinct_from(valor) for coluna, valor in zip(_DIMENSOES, chave)))


def _antes(linha):
    """(periodo_inicio, periodo_fim, id) < os da ``linha``, na forma que usa o índice."""
    return or_(Indicador.periodo_inicio < linha.periodo_inicio,
               and_(Indicador.periodo_inicio == linha.periodo_inicio,
                    or_(Indicador.periodo_fim < linha.periodo_fim,
                        and_(Indicador.periodo_fim == linha.periodo_fim, Indicador.id < linha.id))))


def _anteriores_e_ultimas(consulta, pagina):
    """{id: (anterior, numero_reverso)} das linhas da página, idêntico às funções de janela
    ``MAX(periodo_fim) OVER (série ORDER BY periodo_inicio, periodo_fim, id ROWS BETWEEN
    UNBOUNDED PRECEDING AND 1 PRECEDING)`` e ``ROW_NUMBER() OVER (série ORDER BY ... DESC)``
    sobre a consulta filtrada (só importa se o número reverso é 1)."""
    ordem = lambda r: (r.periodo_inicio, r.periodo_fim, r.id)  # noqa: E731
    por_serie = {}
    for linha in pagina:
        por_serie.setdefault(tuple(getattr(linha, c.key) for c in _DIMENSOES), []).append(linha)
    marcas = {}
    for chave, linhas in por_serie.items():
        serie = consulta.filter(_mesma_serie(chave))
        primeira, ultima_pagina = min(linhas, key=ordem), max(linhas, key=ordem)
        maior = serie.filter(_antes(primeira)).with_entities(func.max(Indicador.periodo_fim)).scalar()
        # Linhas da série entre a primeira e a última da página (empates de início podem
        # deixar alguma fora da página): o máximo corrente segue a ordem da janela.
        trecho = serie.filter(or_(_antes(ultima_pagina), Indicador.id == ultima_pagina.id))             .filter(~_antes(primeira)).with_entities(Indicador.id, Indicador.periodo_inicio,
                                                       Indicador.periodo_fim).all()
        ultima_serie = serie.with_entities(Indicador.id).order_by(
            Indicador.periodo_inicio.desc(), Indicador.periodo_fim.desc(), Indicador.id.desc()).first()
        for item in sorted(trecho, key=ordem):
            marcas[item.id] = (maior, 1 if ultima_serie is not None and item.id == ultima_serie.id else 2)
            maior = item.periodo_fim if maior is None else max(maior, item.periodo_fim)
    return marcas
