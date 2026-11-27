"""Série auditável dos ciclos persistidos da coleta Olho Vivo (US30).

O denominador contém somente ciclos finalizados e classificados: sucesso +
falha própria. Falhas da fonte são excluídas; causas desconhecidas, ciclos
parciais e em andamento ficam explícitos, sem atribuição especulativa.
"""
from datetime import timedelta, timezone
from decimal import Decimal
import re

from sqlalchemy import func

from app.models import ExecucaoColeta


def classificar_ciclo(ciclo):
    if ciclo.estado == "OK":
        return "sucesso"
    if ciclo.estado == "PARCIAL":
        return "parcial"
    if ciclo.estado == "EM_ANDAMENTO":
        return "em_andamento"
    if ciclo.estado != "FALHA":
        return "desconhecida"
    erro = (ciclo.erro or "").strip().upper()
    if not erro:
        return "desconhecida"
    # As causas são produzidas por causa_segura; formatos antigos também existem.
    if erro.startswith(("SPTRANSCONFIGURACAOERROR", "SPTRANSAUTENTICACAOERROR")):
        return "falha_propria"
    http = re.match(r"^SPTRANS(?:_HTTP_|HTTPERROR\s*:\s*)(\d{3})(?:\D|$)", erro)
    if http:
        codigo = int(http.group(1))
        return "falha_fonte" if codigo in (408, 425, 429) or 500 <= codigo <= 599 else "falha_propria"
    if erro.startswith(("SPTRANSTIMEOUTERROR", "SPTRANSCONEXAOERROR",
                        "SPTRANSRESPOSTAINVALIDAERROR")):
        return "falha_fonte"
    if erro.startswith(("OPERATIONALERROR", "INTEGRITYERROR", "DATAERROR",
                        "INTERFACEERROR", "TIMEOUTERROR", "DBAPIERROR",
                        "PROCESSO_INTERROMPIDO", "POSSEPERDIDA")):
        return "falha_propria"
    return "desconhecida"


def serie_sucesso_coleta(inicio, fim, *, janela=timedelta(hours=1)):
    """Agrupa por horário previsto (ou início), em UTC, na faixa [início, fim).

    Não estima ciclos ausentes. O último intervalo pode ser menor que a janela;
    IDs dos ciclos permitem conferir cada contagem contra os registros originais.
    """
    if (inicio is None or fim is None or inicio.tzinfo is None or fim.tzinfo is None
            or inicio.utcoffset() is None or fim.utcoffset() is None):
        raise ValueError("início e fim devem ter fuso horário")
    inicio, fim = inicio.astimezone(timezone.utc), fim.astimezone(timezone.utc)
    if fim <= inicio or fim - inicio > timedelta(days=31):
        raise ValueError("informe uma faixa crescente de até 31 dias")
    if not isinstance(janela, timedelta) or not timedelta(minutes=1) <= janela <= timedelta(days=1):
        raise ValueError("janela deve estar entre 1 minuto e 1 dia")
    instante = func.coalesce(ExecucaoColeta.prevista_em, ExecucaoColeta.iniciada_em)
    ciclos = (ExecucaoColeta.query.filter(ExecucaoColeta.fonte == "OLHO_VIVO",
              instante >= inicio, instante < fim).order_by(instante, ExecucaoColeta.id).all())
    chaves = ("sucesso", "falha_propria", "falha_fonte", "desconhecida", "parcial", "em_andamento")
    pontos = []
    atual = inicio
    while atual < fim:
        fim_ponto = min(atual + janela, fim)
        pontos.append({"inicio": atual.isoformat(), "fim_exclusivo": fim_ponto.isoformat(),
                       "contagens": {chave: 0 for chave in chaves}, "ciclos": []})
        atual = fim_ponto
    for ciclo in ciclos:
        instante_ciclo = (ciclo.prevista_em or ciclo.iniciada_em).astimezone(timezone.utc)
        ponto = pontos[(instante_ciclo - inicio) // janela]
        categoria = classificar_ciclo(ciclo)
        ponto["contagens"][categoria] += 1
        ponto["ciclos"].append({"id": ciclo.id, "estado": ciclo.estado, "categoria": categoria})
    for ponto in pontos:
        contagens = ponto["contagens"]
        denominador = contagens["sucesso"] + contagens["falha_propria"]
        ponto["total_registrado"] = len(ponto["ciclos"])
        ponto["denominador_sem_falhas_fonte"] = denominador
        ponto["percentual_sucesso"] = (float((Decimal(contagens["sucesso"]) * 100 / denominador)
                                              .quantize(Decimal("0.01"))) if denominador else None)
    return {"inicio": inicio.isoformat(), "fim_exclusivo": fim.isoformat(),
            "janela_segundos": janela.total_seconds(), "fonte": "OLHO_VIVO", "pontos": pontos,
            "nota": "Ciclos não registrados não são estimados; parciais, em andamento e causas desconhecidas não entram na taxa."}
