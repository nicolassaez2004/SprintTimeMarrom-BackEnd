"""US30-T03A: consolidação lida por ``GET /saude``; só consulta registros gravados.

Não inicia sondagens nem cálculos longos: lê ``VerificacaoSaude``,
``ExecucaoColeta`` e ``LacunaColeta``. Serviços separados (API, PAINEL, COLETA),
com período, taxas, contagens, cobertura, incidentes, lacunas e limitações.
"""
from datetime import datetime

from sqlalchemy import or_

from app.models import LacunaColeta, VerificacaoSaude
from app.monitoramento.disponibilidade_mensal import (
    LIMITE_LISTAS,
    disponibilidade_mensal,
)


def _ultima(componente, corte):
    V = VerificacaoSaude
    ultima = (V.query.filter(V.componente == componente, V.verificada_em < corte)
              .order_by(V.verificada_em.desc(), V.id.desc()).first())
    if ultima is None:
        return None
    return {"instante": ultima.verificada_em.isoformat(), "estado": ultima.estado,
            "detalhe": ultima.detalhe, "status_http": ultima.status_http,
            "latencia_ms": ultima.latencia_ms, "alvo": ultima.alvo}


def consultar_saude(mes, *, agora):
    relatorio = disponibilidade_mensal(mes, agora=agora)
    inicio = datetime.fromisoformat(relatorio["inicio_utc"])
    corte = datetime.fromisoformat(relatorio["corte_exclusivo_utc"])
    for componente in ("API", "PAINEL"):
        ultima = _ultima(componente, corte)
        if ultima is not None:
            ultima["no_periodo"] = datetime.fromisoformat(ultima["instante"]) >= inicio
        relatorio["servicos"][componente]["ultima_medicao"] = ultima
    L = LacunaColeta
    consulta = L.query.filter(L.fonte == "OLHO_VIVO", L.inicio < corte,
                              or_(L.fim.is_(None), L.fim > inicio))
    total = consulta.count()
    lacunas = consulta.order_by(L.inicio.desc(), L.id.desc()).limit(LIMITE_LISTAS).all()
    coleta = relatorio["servicos"]["COLETA"]
    coleta["incidentes"] = {
        "total": total, "truncado": total > len(lacunas),
        "itens": [{"id": item.id, "inicio": item.inicio.isoformat(),
                   "fim": item.fim.isoformat() if item.fim is not None and item.fim <= corte else None,
                   "estado_no_corte": "ENCERRADA" if item.fim is not None and item.fim <= corte else "ABERTA",
                   "causa": item.causa, "tipo": "LACUNA_COLETA"} for item in lacunas],
    }
    relatorio["limites"] = [
        "API e PAINEL: minutos sem sondagem são lacunas de monitoramento, não sucesso nem queda.",
        "Sondagem do PAINEL confirma a entrega da página, não a execução do JavaScript nem dos gráficos.",
        "Sondagem da API comprova a rota configurada; /health não testa o banco.",
        "Monitor no mesmo host/banco: queda do banco também impede gravar a sondagem (vira lacuna).",
        "COLETA: falha da fonte só é excluída com evidência; timeout e conexão ficam indeterminados.",
        "Última medição pode ser anterior ao período quando não houve sondagem nele (no_periodo=false).",
    ]
    return relatorio
