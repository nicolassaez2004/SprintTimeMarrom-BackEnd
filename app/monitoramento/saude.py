"""Consolidação de disponibilidade e incidentes para a consulta pública US30."""
from datetime import datetime

from sqlalchemy import or_

from app.models import LacunaColeta, VerificacaoDisponibilidade
from app.monitoramento.disponibilidade_mensal import disponibilidade_mensal

LIMITE_INCIDENTES = 100


def consultar_saude(mes, *, agora):
    relatorio = disponibilidade_mensal(mes, agora=agora)
    inicio = datetime.fromisoformat(relatorio["inicio_utc"])
    corte = datetime.fromisoformat(relatorio["corte_exclusivo_utc"])
    V = VerificacaoDisponibilidade
    for alvo, dados in relatorio["servicos"].items():
        ultima = (V.query.filter(V.alvo == alvo, V.instante >= inicio, V.instante < corte)
                  .order_by(V.instante.desc(), V.id.desc()).first())
        dados["ultima_medicao"] = ({"instante": ultima.instante.isoformat(),
            "duracao_ms": ultima.duracao_ms, "sucesso": ultima.sucesso,
            "status_http": ultima.status_http, "resultado": ultima.resultado}
            if ultima is not None else None)
    L = LacunaColeta
    consulta = L.query.filter(L.fonte == "OLHO_VIVO", L.inicio < corte,
                               or_(L.fim.is_(None), L.fim > inicio))
    total = consulta.count()
    lacunas = consulta.order_by(L.inicio.desc(), L.id.desc()).limit(LIMITE_INCIDENTES).all()
    relatorio["incidentes_coleta"] = {
        "total": total, "limite": LIMITE_INCIDENTES, "truncado": total > len(lacunas),
        "itens": [{"id": item.id, "inicio": item.inicio.isoformat(),
                   "fim": item.fim.isoformat() if item.fim is not None and item.fim <= corte else None,
                   "estado_no_corte": "ENCERRADA" if item.fim is not None and item.fim <= corte else "ABERTA",
                   "fonte": item.fonte} for item in lacunas],
    }
    relatorio["limites"] = [
        "A taxa usa minutos observados; períodos sem sondagem não são contados como sucesso.",
        "A última medição pertence ao período analisado e não comprova disponibilidade atual.",
        "Falha de sondagem não identifica automaticamente sua causa.",
        "Incidentes são lacunas de coleta registradas; ausência de registro não prova ausência de falha.",
    ]
    return relatorio
