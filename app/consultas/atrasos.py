"""QueryService — ranking mensal dos trechos com maior aumento de atraso (US19-T03).

Só lê os agregados ``AUMENTO_ATRASO_TRECHO_MENSAL`` gravados por
``app.indicadores.propagacao.agregar_mes``; nenhum atraso é calculado aqui.
Trechos sem valor (NULL) nunca entram no ranking e são apenas contados.
"""

from app import db
from app.indicadores.propagacao import INDICADOR_MENSAL, UNIDADE, limites_mes, versao_mensal
from app.models import IndicadorOperacional, Trecho

LIMITE_RANKING = 20
CRITERIO = "valor decrescente; empate por trecho_id crescente"


def _iso(instante):
    return instante.isoformat().replace("+00:00", "Z")


def ranking_mensal(mes, metrica, *, linha=None, sentido=None, versao_gtfs_id=None):
    inicio, fim = limites_mes(mes)
    I = IndicadorOperacional
    base = db.session.query(I).filter(
        I.indicador == INDICADOR_MENSAL, I.versao_calculo == versao_mensal(metrica),
        I.periodo_inicio == inicio, I.periodo_fim == fim)
    for coluna, valor in ((I.linha_codigo, linha), (I.sentido, sentido), (I.versao_gtfs_id, versao_gtfs_id)):
        if valor is not None:
            base = base.filter(coluna == valor)
    sem_valor = base.filter(I.valor.is_(None)).count()
    com_valor = base.filter(I.valor.isnot(None))
    linhas = (com_valor.join(Trecho, Trecho.id == I.trecho_id).add_entity(Trecho)
              .order_by(I.valor.desc(), I.trecho_id.asc()).limit(LIMITE_RANKING).all())
    return {
        "mes": mes, "periodo": {"inicio": _iso(inicio), "fim": _iso(fim)}, "fuso": "America/Sao_Paulo",
        "metrica": metrica, "unidade": UNIDADE, "limite": LIMITE_RANKING, "ordenacao": CRITERIO,
        "trechos_com_valor": com_valor.count(), "trechos_sem_valor": sem_valor,
        "dados": [{
            "posicao": posicao,
            "trecho": {"id": t.id, "linha": t.linha_codigo, "sentido": t.sentido, "trajeto": t.trajeto,
                       "ordem": t.ordem, "parada_origem": t.parada_origem_codigo,
                       "parada_destino": t.parada_destino_codigo},
            "versao_gtfs_id": r.versao_gtfs_id, "aumento_atraso": r.valor, "unidade": r.unidade,
            "amostras": r.contagem_numerador, "amostras_total": r.contagem_denominador,
            "cobertura": (r.qualidade or {}).get("cobertura"),
            "versao_calculo": r.versao_calculo, "versao_correspondencia": r.versao_correspondencia,
            "versao_conjunto": r.versao_conjunto, "qualidade": r.qualidade or {},
        } for posicao, (r, t) in enumerate(linhas, start=1)],
    }
