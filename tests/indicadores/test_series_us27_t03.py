"""US27-T03: a consulta de séries otimizada devolve exatamente o mesmo que a anterior.

``consultar_series_antiga`` é a implementação da ``main`` antes da US27-T03 (cópia literal),
usada como referência. Dados SINTÉTICOS variados: janelas de 15 min e de 1 h sobrepostas,
valores nulos, lacunas, dois sentidos, dois tipos e linhas por parada.
"""
from datetime import datetime, time, timedelta, timezone
import random

import pytest
from sqlalchemy import and_, func, or_  # noqa: F401 (usados pela referência)

from app import db
from app.core.tempo import FUSO
from app.indicadores.series import _janelas_horarias, _ou_balanceado, _iso, consultar_series
from app.indicadores.service import consulta_base, serializar
from app.models import ExecucaoColeta, IndicadorOperacional as Indicador, Parada

UTC = timezone.utc
T0 = datetime(2026, 11, 2, 3, tzinfo=UTC)  # 00:00 em São Paulo


def consultar_series_antiga(filtros):
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



@pytest.fixture
def base(programacao):
    versao, linha, _, _ = programacao
    db.session.add_all(Parada(versao_gtfs_id=versao.id, codigo=c, nome=c, latitude=-23.5, longitude=-46.6)
                       for c in ("P1", "P2"))
    aleatorio = random.Random(27)
    seq = 0
    for sentido in ("0", "1"):
        for tipo, unidade, parada in (("CUMPRIMENTO_VIAGENS", "%", None), ("PONTUALIDADE_VIAGENS", "%", None),
                                       ("ESPERA_ESTIMADA", "min", "P1"), ("ESPERA_ESTIMADA", "min", "P2")):
            t = T0
            while t < T0 + timedelta(days=4):
                if aleatorio.random() < 0.85:  # 15% de lacunas
                    largura = timedelta(minutes=60 if aleatorio.random() < 0.1 else 15)  # sobreposições
                    seq += 1
                    db.session.add(Indicador(
                        id=f"s{seq:05d}", chave_resultado=f"s{seq:05d}", versao_gtfs_id=versao.id,
                        linha_codigo=linha.codigo, sentido=sentido, parada_codigo=parada, indicador=tipo,
                        unidade=unidade, valor=None if aleatorio.random() < 0.1 else round(aleatorio.uniform(0, 100), 2),
                        periodo_inicio=t, periodo_fim=t + largura, versao_conjunto="v1", publicado_em=t))
                t += timedelta(minutes=15)
    db.session.add(ExecucaoColeta(estado="OK", iniciada_em=T0 - timedelta(hours=1), novos_persistidos=1))
    db.session.commit()
    return linha.codigo


def _filtros(linha, **extra):
    padrao = {"linha": linha, "sentido": "0", "tipo": "CUMPRIMENTO_VIAGENS", "parada": None, "trecho": None,
              "versao_gtfs_id": None, "inicio": None, "fim": None, "hora_inicio": None, "hora_fim": None,
              "page": 1, "page_size": 50}
    return {**padrao, **extra}


CASOS = [
    {},
    {"page": 3},
    {"page": 9, "page_size": 37},
    {"sentido": "1", "tipo": "PONTUALIDADE_VIAGENS", "page": 2, "page_size": 100},
    {"inicio": T0 + timedelta(hours=7), "fim": T0 + timedelta(days=2, hours=5)},
    {"inicio": T0 + timedelta(days=1), "fim": T0 + timedelta(days=3), "page": 4},
    {"hora_inicio": "07:00", "hora_fim": "09:30", "inicio": T0, "fim": T0 + timedelta(days=4)},
    {"hora_inicio": "22:00", "hora_fim": "02:00", "inicio": T0, "fim": T0 + timedelta(days=4), "page": 2},
    {"tipo": "ESPERA_ESTIMADA", "parada": "P2", "page": 5},
    {"tipo": "ESPERA_ESTIMADA", "parada": None},                  # parada sem filtro: só a linha
    {"tipo": None, "sentido": None, "page": 11, "page_size": 100},  # todos os tipos e sentidos
    {"inicio": T0 + timedelta(days=10), "fim": T0 + timedelta(days=11)},  # período vazio
]


@pytest.mark.parametrize("caso", CASOS, ids=[str(i) for i in range(len(CASOS))])
def test_resposta_identica_a_implementacao_anterior(base, caso):
    filtros = _filtros(base, **caso)
    assert consultar_series(dict(filtros)) == consultar_series_antiga(dict(filtros))


def test_paginas_cobrem_o_resultado_sem_repetir(base):
    filtros = _filtros(base, tipo=None, sentido=None, page_size=100)
    total = consultar_series(dict(filtros))["paginacao"]["total"]
    ids = []
    for pagina in range(1, total // 100 + 2):
        ids += [d["id"] for d in consultar_series({**filtros, "page": pagina})["dados"]]
    assert len(ids) == len(set(ids)) == total


@pytest.fixture
def com_empates(base, programacao):
    """Janelas da mesma série que começam no mesmo instante com fins diferentes (15 min e 1 h)."""
    versao, linha, _, _ = programacao
    for n in range(40):
        t = T0 + timedelta(hours=n)
        for largura, sufixo in ((timedelta(minutes=45), "a"), (timedelta(minutes=30), "b")):
            db.session.add(Indicador(
                id=f"e{n:03d}{sufixo}", chave_resultado=f"e{n:03d}{sufixo}", versao_gtfs_id=versao.id,
                linha_codigo=linha.codigo, sentido="0", indicador="CUMPRIMENTO_VIAGENS", unidade="%",
                valor=50.0, periodo_inicio=t, periodo_fim=t + largura, versao_conjunto="v1", publicado_em=t))
    db.session.commit()
    return base


@pytest.mark.parametrize("tamanho", [7, 13, 50])
def test_empates_em_todas_as_paginas(com_empates, tamanho):
    filtros = _filtros(com_empates, page_size=tamanho)
    total = consultar_series_antiga(dict(filtros))["paginacao"]["total"]
    for pagina in range(1, total // tamanho + 2):
        caso = {**filtros, "page": pagina}
        assert consultar_series(dict(caso)) == consultar_series_antiga(dict(caso)), pagina


def _indicadores_antiga(filtros):
    """``consultar_indicadores`` da main antes da US27-T03 (lê as linhas completas para ordenar)."""
    from app.indicadores.service import filtrar_periodo

    consulta = filtrar_periodo(consulta_base(filtros), filtros)
    registros = consulta.order_by(Indicador.periodo_inicio.asc(), Indicador.linha_codigo.asc(),
                                  Indicador.sentido.asc(), Indicador.indicador.asc(), Indicador.id.asc()) \
        .offset((filtros["page"] - 1) * filtros["page_size"]).limit(filtros["page_size"]).all()
    return {"dados": [serializar(r) for r in registros],
            "paginacao": {"page": filtros["page"], "page_size": filtros["page_size"], "total": consulta.count()}}


@pytest.mark.parametrize("caso", CASOS, ids=[str(i) for i in range(len(CASOS))])
def test_api_indicadores_identica_a_implementacao_anterior(com_empates, caso):
    from app.indicadores.service import consultar_indicadores

    filtros = _filtros(com_empates, **caso)
    assert consultar_indicadores(dict(filtros)) == _indicadores_antiga(dict(filtros))
