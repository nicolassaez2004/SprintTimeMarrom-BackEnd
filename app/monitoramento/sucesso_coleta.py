"""US30-T01A: taxa de sucesso dos ciclos da coleta Olho Vivo, auditável.

Classificação de cada linha de ``ExecucaoColeta`` (um ciclo; as retentativas
do mesmo ciclo ficam em ``tentativas`` da mesma linha e não contam de novo):

- ``sucesso``: estado OK. Inclui ciclo válido sem veículos (``recebidos = 0``):
  a consulta aconteceu e foi gravada; isso é diferente de ciclo não registrado.
- ``falha_fonte`` (exige evidência de que a SPTrans respondeu com erro):
  ``SPTRANS_HTTP_5xx`` e ``SPTransRespostaInvalidaError``.
- ``falha_propria``: configuração/credencial (``SPTransConfiguracaoError``,
  ``SPTransAutenticacaoError``), HTTP 4xx do nosso pedido (exceto 408/425/429),
  banco/gravação e processo interrompido ou sem posse do coletor.
- ``indeterminada``: timeout e erro de conexão (podem ser rede local, DNS ou
  fonte), HTTP 408/425/429, causa vazia/desconhecida, ``PARCIAL`` legado e
  ``EM_ANDAMENTO``. Nunca viram "falha da SPTrans" por suposição.

Fórmula mensal (``taxa_coleta``)::

    esperados       = ⌊(corte − início) / intervalo configurado⌋
    nao_registrados = max(0, esperados − registrados)   # processo parado, etc.
    denominador     = esperados − falhas_fonte          # só a fonte é excluída
    percentual      = sucessos / denominador            # conservador
    percentual_max  = sucessos / (denominador − indeterminados)

Indeterminados e não registrados permanecem no denominador conservador; o
percentual máximo mostra o teto caso todos fossem da fonte. Denominador zero
gera ``percentual = None`` com motivo. Limite: ``erro`` guarda só a última causa
do ciclo, e o intervalo esperado é o configurado hoje.
"""
import re
from collections import Counter
from datetime import timedelta, timezone
from decimal import Decimal

from app.models import ExecucaoColeta

CATEGORIAS = ("sucesso", "falha_propria", "falha_fonte", "indeterminada")
_PROPRIAS = ("SPTRANSCONFIGURACAOERROR", "SPTRANSAUTENTICACAOERROR", "OPERATIONALERROR",
             "INTEGRITYERROR", "DATAERROR", "INTERFACEERROR", "DBAPIERROR", "PROGRAMMINGERROR",
             "PROCESSO_INTERROMPIDO", "POSSEPERDIDA")
_FOLGA = timedelta(minutes=5)  # prevista_em e iniciada_em diferem por segundos


def classificar_ciclo(ciclo):
    """Categoria e regra aplicada (para auditoria)."""
    if ciclo.estado == "OK":
        return "sucesso", "estado_ok"
    if ciclo.estado != "FALHA":
        return "indeterminada", f"estado_{(ciclo.estado or 'nulo').lower()}"
    erro = (ciclo.erro or "").strip().upper()
    http = re.match(r"^SPTRANS(?:_HTTP_|HTTPERROR\s*:\s*)(\d{3})(?:\D|$)", erro)
    if http:
        codigo = int(http.group(1))
        if 500 <= codigo <= 599:
            return "falha_fonte", "http_5xx_da_fonte"
        if codigo in (408, 425, 429):
            return "indeterminada", "http_408_425_429"
        return "falha_propria", "http_4xx_do_pedido"
    if erro.startswith("SPTRANSRESPOSTAINVALIDAERROR"):
        return "falha_fonte", "resposta_fora_do_contrato"
    if erro.startswith(("SPTRANSTIMEOUTERROR", "SPTRANSCONEXAOERROR", "TIMEOUTERROR")):
        return "indeterminada", "timeout_ou_conexao"
    if erro.startswith(_PROPRIAS):
        return "falha_propria", "configuracao_banco_ou_processo"
    return "indeterminada", "causa_ausente_ou_desconhecida"


def _pct(numerador, denominador):
    if not denominador:
        return None
    return float((Decimal(numerador) * 100 / denominador).quantize(Decimal("0.0001")))


def ciclos_no_periodo(inicio, corte):
    """Ciclos OLHO_VIVO com instante previsto (ou início) em ``[inicio, corte)``."""
    E = ExecucaoColeta
    linhas = (E.query.with_entities(E.id, E.estado, E.erro, E.prevista_em, E.iniciada_em, E.lacuna_id,
                                    E.recebidos)
              .filter(E.fonte == "OLHO_VIVO", E.iniciada_em >= inicio - _FOLGA,
                      E.iniciada_em < corte + _FOLGA).all())
    return [c for c in linhas if inicio <= (c.prevista_em or c.iniciada_em) < corte]


def taxa_coleta(inicio, corte, intervalo_seg):
    """Componentes e percentuais da coleta em ``[inicio, corte)`` (UTC)."""
    ciclos = ciclos_no_periodo(inicio, corte)
    contagem, regras = Counter(), Counter()
    for ciclo in ciclos:
        categoria, regra = classificar_ciclo(ciclo)
        contagem[categoria] += 1
        regras[regra] += 1
    esperados = int((corte - inicio).total_seconds() // intervalo_seg) if corte > inicio else 0
    registrados = len(ciclos)
    nao_registrados = max(0, esperados - registrados)
    denominador = esperados - contagem["falha_fonte"]
    indeterminados = contagem["indeterminada"] + nao_registrados
    motivo = None
    if esperados == 0:
        motivo = "periodo_sem_ciclos_esperados"
    elif denominador <= 0:
        motivo = "todos_os_ciclos_esperados_tiveram_falha_da_fonte"
    previstas = Counter(c.prevista_em for c in ciclos if c.prevista_em is not None)
    return {
        "intervalo_configurado_seg": intervalo_seg,
        "ciclos_esperados": esperados, "ciclos_registrados": registrados,
        "ciclos_nao_registrados": nao_registrados,
        "ciclos_registrados_alem_do_esperado": max(0, registrados - esperados),
        "ciclos_com_prevista_em_repetida": sum(n - 1 for n in previstas.values() if n > 1),
        "sucessos": contagem["sucesso"], "falhas_proprias": contagem["falha_propria"],
        "falhas_fonte_excluidas": contagem["falha_fonte"],
        "indeterminados_registrados": contagem["indeterminada"],
        "indeterminados_total": indeterminados,
        "sucessos_sem_veiculos": sum(1 for c in ciclos if c.estado == "OK" and c.recebidos == 0),
        "regras_aplicadas": dict(sorted(regras.items())),
        "lacunas_vinculadas": sorted({c.lacuna_id for c in ciclos if c.lacuna_id}),
        "numerador": contagem["sucesso"], "denominador": max(denominador, 0),
        "percentual": _pct(contagem["sucesso"], denominador) if denominador > 0 else None,
        "percentual_maximo": (_pct(contagem["sucesso"], denominador - indeterminados)
                              if denominador - indeterminados > 0 else None),
        "motivo_sem_percentual": motivo,
        "formula": "sucessos / (esperados - falhas_fonte); indeterminados e não registrados "
                   "permanecem no denominador; percentual_maximo os exclui",
    }


def serie_sucesso_coleta(inicio, fim, *, janela=timedelta(hours=1), intervalo_seg=30):
    """Série por janela em UTC, com IDs por categoria para conferir contra os ciclos."""
    if (inicio is None or fim is None or inicio.tzinfo is None or fim.tzinfo is None
            or inicio.utcoffset() is None or fim.utcoffset() is None):
        raise ValueError("início e fim devem ter fuso horário")
    inicio, fim = inicio.astimezone(timezone.utc), fim.astimezone(timezone.utc)
    if fim <= inicio or fim - inicio > timedelta(days=31):
        raise ValueError("informe uma faixa crescente de até 31 dias")
    if not isinstance(janela, timedelta) or not timedelta(minutes=1) <= janela <= timedelta(days=1):
        raise ValueError("janela deve estar entre 1 minuto e 1 dia")
    pontos, atual = [], inicio
    while atual < fim:
        fim_ponto = min(atual + janela, fim)
        ponto = {"inicio": atual.isoformat(), "fim_exclusivo": fim_ponto.isoformat(),
                 **taxa_coleta(atual, fim_ponto, intervalo_seg)}
        ponto["ciclos"] = {c: [] for c in CATEGORIAS}
        for ciclo in ciclos_no_periodo(atual, fim_ponto):
            ponto["ciclos"][classificar_ciclo(ciclo)[0]].append(ciclo.id)
        pontos.append(ponto)
        atual = fim_ponto
    return {"inicio": inicio.isoformat(), "fim_exclusivo": fim.isoformat(),
            "janela_segundos": janela.total_seconds(), "fonte": "OLHO_VIVO", "pontos": pontos}
