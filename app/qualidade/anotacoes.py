"""``qualidade_coleta``: resumo público da qualidade da coleta (US26-T02).

Fonte única: o bloco ``qualidade.falhas_coleta`` gravado com cada indicador na
publicação (US26-T01, ``FalhasColeta``). Nada é recalculado na consulta: API,
mapa e CSV da mesma versão mostram exatamente a qualidade publicada com o valor.
Agregações (faixa/dia/mês, territórios) somam as contagens dos blocos das suas
janelas — nunca a média de percentuais — com a mesma regra (``classificar_falhas``).
"""

from datetime import datetime
from decimal import Decimal

from app.qualidade.service import (
    ESCOPO_FALHAS, LIMITE_INCOMPLETO_PCT, VERSAO_REGRA_FALHAS, classificar_falhas,
)

LIMITE = float(LIMITE_INCOMPLETO_PCT)
_CONTAGENS = ("ciclos_esperados", "ciclos_registrados", "ciclos_com_dados", "ciclos_falha",
              "ciclos_em_andamento", "falhas_da_fonte", "sem_registro", "falhas", "denominador",
              "janelas_15min", "janelas_sem_expectativa")


def _instante(valor):
    return datetime.fromisoformat(valor.replace("Z", "+00:00")) if isinstance(valor, str) else valor


def _base(item):
    return {"inicio": item.get("inicio"), "fim": item.get("fim"),
            "versao_gtfs_id": item.get("versao_gtfs_id"), "escopo": ESCOPO_FALHAS,
            "versao_regra": VERSAO_REGRA_FALHAS, "limite_incompleto_percentual": LIMITE}


def _indeterminada(item, motivo):
    """Sem bloco publicado não se afirma nada: percentual e marca ficam nulos."""
    return {**_base(item), "situacao": "NAO_REGISTRADA", "percentual_falhas": None,
            "incompleto": None, "motivo": motivo, "causas": [], "contagens": None}


def resumo_coleta(blocos, item):
    """Resumo de ``item`` a partir dos blocos ``falhas_coleta`` das janelas que o compõem."""
    if item.get("inicio") is None or item.get("fim") is None:
        return _indeterminada(item, "SEM_PERIODO_ASSOCIADO")
    if not blocos or any(not isinstance(b, dict) for b in blocos):
        # Publicado antes da US26-T01 (ou parte do agregado): só o reprocessamento grava o bloco.
        return _indeterminada(item, "PUBLICADO_SEM_QUALIDADE_DE_COLETA")
    inicio, fim = _instante(item["inicio"]), _instante(item["fim"])
    janelas = {}
    for bloco in blocos:  # coleta global: a mesma janela aparece em vários indicadores
        a, b = _instante(bloco["inicio"]), _instante(bloco["fim"])
        if a < inicio or b > fim:
            return _indeterminada(item, "PERIODO_DIVERGENTE")
        janelas.setdefault((a, b), bloco)
    unicos = list(janelas.values())
    soma = {k: sum(b.get(k) or 0 for b in unicos) for k in _CONTAGENS}
    sem_expectativa = sorted({b["motivo"] for b in unicos if b["situacao"] == "SEM_EXPECTATIVA"})
    situacao, motivo = classificar_falhas(
        soma["falhas"], soma["denominador"], soma["ciclos_com_dados"],
        sem_expectativa[0] if len(sem_expectativa) == 1 else "MULTIPLOS_MOTIVOS")
    medido = situacao != "SEM_EXPECTATIVA"
    pct = (Decimal(soma["falhas"]) * 100 / soma["denominador"]).quantize(Decimal("0.01")) if medido else None
    return {
        **_base(item), "situacao": situacao,
        "percentual_falhas": float(pct) if medido else None,
        "incompleto": soma["falhas"] * 100 > LIMITE_INCOMPLETO_PCT * soma["denominador"] if medido else None,
        "motivo": motivo,
        "causas": sorted({l["causa"] for b in unicos for l in b.get("lacunas") or ()}),
        "contagens": soma,
    }


def adicionar_qualidade_coleta(itens):
    """Anexa ``qualidade_coleta`` aos itens que ainda não o têm (somente leitura)."""
    for item in itens:
        if "qualidade_coleta" not in item:
            bloco = (item.get("qualidade") or {}).get("falhas_coleta")
            item["qualidade_coleta"] = resumo_coleta([bloco] if bloco else [], item)
    return itens
