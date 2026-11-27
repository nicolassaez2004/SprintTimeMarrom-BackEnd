"""US20-T02B: executa um pedido ``RECALCULO_INDICADOR`` com os cálculos existentes.

Chamado pelo worker (``app.jobs.executor``) depois da reserva. Usa as mesmas
funções e contextos da agenda US23-T03 (US14-US18) para o indicador, escopo e
janela gravados no pedido, e grava por ``gravar_indicador`` **sem commit**: o
executor confirma resultados e PRONTO juntos, só se ainda tiver a reserva.

Não altera brutos nem o pedido (chave, versões, escopo e parâmetros).

US20-T02C: ``calcular_pedido`` (só leituras) e ``publicar_pedido`` (upsert pela
``chave_resultado``) são as duas fases do worker, que valida a posse entre elas.
"""
from datetime import datetime, timedelta, timezone

from app.core.tempo import FUSO
from app.indicadores.agenda import PUBLICADOS, _contextos
from app.indicadores.cumprimento import calcular_indicador_cumprimento, gravar_indicador
from app.indicadores.espera import ESPERA, EXCESSO, CalculoEspera
from app.indicadores.frequencia import OBSERVADA, PROGRAMADA, CalculoFrequencia
from app.indicadores.pontualidade import calcular_indicador_pontualidade
from app.indicadores.regularidade import REGULARIDADE, CalculoRegularidade
from app.qualidade.service import FalhasColeta

REGRA_VIGENTE = dict(PUBLICADOS)  # indicador -> versão da regra implementada no código
POR_JANELA = {"CUMPRIMENTO_VIAGENS": calcular_indicador_cumprimento,
              "PONTUALIDADE_VIAGENS": calcular_indicador_pontualidade}
POR_DIA = {OBSERVADA: CalculoFrequencia, PROGRAMADA: CalculoFrequencia,
           REGULARIDADE: CalculoRegularidade, ESPERA: CalculoEspera, EXCESSO: CalculoEspera}


class RegraIndisponivel(ValueError):
    """O código em execução não implementa a versão de regra pedida."""


def _no_escopo(campos, pedido):
    escopo = pedido.parametros["escopo"]
    return (campos["indicador"] == pedido.parametros["indicador"]
            and pedido.periodo_inicio <= campos["periodo_inicio"]
            and campos["periodo_fim"] <= pedido.periodo_fim
            and campos.get("parada_codigo") == escopo.get("parada")
            and campos.get("trecho_id") == escopo.get("trecho"))


def calcular_pedido(pedido, agora=None):
    """Calcula, sem gravar, os resultados do indicador, escopo e janela do pedido."""
    agora = agora or datetime.now(timezone.utc)
    indicador, escopo = pedido.parametros["indicador"], pedido.parametros["escopo"]
    if REGRA_VIGENTE.get(indicador) != pedido.versao_regra:
        raise RegraIndisponivel(f"{indicador} {pedido.versao_regra}")
    dia = pedido.periodo_inicio.astimezone(FUSO).date()
    contextos = [c for c in _contextos({dia - timedelta(days=1), dia})
                 if c[1] == escopo["linha"] and c[2] == escopo["sentido"]
                 and escopo.get("gtfs") in (None, c[3])]
    if indicador in POR_JANELA:
        calcular = POR_JANELA[indicador]
        resultados = [calcular(linha=linha, sentido=sentido, inicio=pedido.periodo_inicio,
                               fim=pedido.periodo_fim, versao_gtfs_id=gtfs,
                               versao_correspondencia=corresp, versao_reconstrucao=recon)
                      for linha, sentido, gtfs, corresp, recon in sorted({c[1:] for c in contextos})]
    else:
        Calculo = POR_DIA[indicador]
        resultados = [campos for data, linha, sentido, gtfs, corresp, recon in contextos
                      for campos in Calculo(linha=linha, sentido=sentido, data_operacao=data,
                                            versao_gtfs_id=gtfs, versao_correspondencia=corresp,
                                            versao_reconstrucao=recon, agora=agora).calcular()]
    return [campos for campos in resultados if _no_escopo(campos, pedido)]


def publicar_pedido(pedido, resultados, agora=None):
    """Grava (sem commit) os resultados; repetir não duplica (upsert por ``chave_resultado``)."""
    agora = agora or datetime.now(timezone.utc)
    falhas = FalhasColeta(agora=agora)
    for campos in resultados:
        gravar_indicador(campos, commit=False, falhas=falhas).publicado_em = agora
    return len(resultados)


def recalcular_pedido(pedido, agora=None):
    """Calcula e grava (sem commit) numa só fase; retorna quantos registros."""
    return publicar_pedido(pedido, calcular_pedido(pedido, agora), agora)
