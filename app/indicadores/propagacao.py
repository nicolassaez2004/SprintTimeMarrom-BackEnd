"""US19-T01: variação do atraso entre paradas consecutivas da mesma viagem.

Regras (docs/US19-T01.md):

- Atraso numa parada = passagem estimada (US15-T01) − horário previsto da mesma
  partida programada (US04) naquela parada, em segundos.
- Variação no trecho = atraso no destino − atraso na origem: positivo = o atraso
  cresceu, negativo = diminuiu, zero = constante. Nunca a simples diferença
  entre os horários das duas passagens.
- Horário previsto vem no formato GTFS (``HH:MM:SS``, podendo passar de 24h),
  contado a partir da meia-noite de São Paulo da data operacional da partida,
  como na geração de partidas da US04-T02A. O da primeira parada precisa
  coincidir com ``saida_prevista``: é o que garante mesma partida e mesma data.
- Faltando passagem ou horário previsto, o valor fica ``None`` com motivo.
  Oferta por frequência não tem horário fixo e não recebe atraso.
- Correlação entre viagens e ranking mensal são das próximas tarefas da US19.
"""

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Optional

from app.core.tempo import FUSO
from app.programacao.gtfs import _segundos_gtfs

VERSAO_CALCULO = "propagacao-atraso-1.0"
UNIDADE = "s"


@dataclass(frozen=True)
class VariacaoTrecho:
    trecho_id: str
    ordem: int
    parada_origem: str
    parada_destino: str
    atraso_origem_seg: Optional[float]
    atraso_destino_seg: Optional[float]
    variacao_seg: Optional[float]
    motivo: Optional[str] = None
    passagem_origem_id: Optional[str] = None
    passagem_destino_id: Optional[str] = None


@dataclass(frozen=True)
class PropagacaoViagem:
    """Variações por trecho com as referências e versões usadas."""
    viagem_observada_id: str
    partida_programada_id: str
    importacao_id: str
    versao_gtfs_id: str
    data_operacao: object
    versao_reconstrucao: str
    versao_correspondencia: str
    versao_estimativa: Optional[str]
    trechos: tuple
    unidade: str = UNIDADE
    versao_calculo: str = VERSAO_CALCULO


def _conferir_percurso(trechos, importacao):
    trechos = sorted(trechos, key=lambda t: t.ordem)
    if not trechos:
        raise ValueError("percurso sem trechos")
    esperado = (importacao.versao_gtfs_id, importacao.linha_codigo, importacao.sentido)
    if any((t.versao_gtfs_id, t.linha_codigo, t.sentido) != esperado for t in trechos):
        raise ValueError("trecho de outra versão GTFS, linha ou sentido")
    if len({t.trajeto for t in trechos}) > 1:
        raise ValueError("trechos de trajetos diferentes")
    if [t.ordem for t in trechos] != list(range(1, len(trechos) + 1)) or any(
            a.parada_destino_codigo != b.parada_origem_codigo for a, b in zip(trechos, trechos[1:])):
        raise ValueError("trechos não formam uma sequência de paradas consecutivas")
    return trechos


def _conferir_passagens(passagens, viagem, versao_gtfs_id, paradas):
    if any(p.viagem_observada_id != viagem.id for p in passagens):
        raise ValueError("passagem de outra viagem")
    if any(p.versao_gtfs_id != versao_gtfs_id for p in passagens):
        raise ValueError("passagem de outra versão GTFS")
    if len({p.versao_estimativa for p in passagens}) > 1:
        raise ValueError("passagens de versões de estimativa diferentes")
    codigos = [p.parada_codigo for p in passagens]
    if len(codigos) != len(set(codigos)) or not set(codigos) <= set(paradas):
        raise ValueError("passagem repetida ou de parada fora do percurso")
    return {p.parada_codigo: p for p in passagens}


def _instantes_previstos(previstos, partida, primeira_parada):
    """``{parada: 'HH:MM:SS'}`` → instantes UTC; confere a partida pela primeira parada."""
    if not previstos:
        return {}
    meia_noite = datetime.combine(partida.importacao.data_operacao, time(), FUSO)
    instantes = {codigo: meia_noite + timedelta(seconds=_segundos_gtfs(valor, codigo))
                 for codigo, valor in previstos.items() if valor}
    if instantes.get(primeira_parada) != partida.saida_prevista:
        raise ValueError("horários previstos não correspondem à saída da partida programada")
    return instantes


def calcular_propagacao(viagem, trechos, passagens, previstos):
    """Variação do atraso em cada trecho do percurso de uma ``ViagemObservada``.

    ``trechos``: ``Trecho`` de um único trajeto da linha/sentido/versão da partida.
    ``passagens``: ``PassagemEstimada`` da viagem (uma versão de estimativa).
    ``previstos``: ``{parada_codigo: 'HH:MM:SS'}`` da mesma partida (GTFS ``stop_times``).
    Não grava nada e não lê posições brutas.
    """
    partida = viagem.partida_programada
    importacao = partida.importacao
    trechos = _conferir_percurso(trechos, importacao)
    sequencia = [trechos[0].parada_origem_codigo] + [t.parada_destino_codigo for t in trechos]
    por_parada = _conferir_passagens(passagens, viagem, importacao.versao_gtfs_id, sequencia)
    frequencia = partida.tipo_oferta != "FIXA"
    instantes = {} if frequencia else _instantes_previstos(previstos, partida, sequencia[0])
    repetidas = {c for c in sequencia if sequencia.count(c) > 1}

    def atraso(codigo):
        passagem, previsto = por_parada.get(codigo), instantes.get(codigo)
        if frequencia:
            return None, "oferta_por_frequencia"
        if codigo in repetidas:
            return None, "parada_repetida_no_percurso"
        if previsto is None:
            return None, "horario_previsto_ausente"
        if passagem is None:
            return None, "passagem_ausente"
        if None in (passagem.posicao_anterior_id, passagem.posicao_posterior_id):
            return None, "passagem_sem_rastreabilidade"
        return (passagem.instante - previsto).total_seconds(), None

    resultado = []
    for t in trechos:
        (origem, motivo_o), (destino, motivo_d) = atraso(t.parada_origem_codigo), atraso(t.parada_destino_codigo)
        ida, volta = por_parada.get(t.parada_origem_codigo), por_parada.get(t.parada_destino_codigo)
        resultado.append(VariacaoTrecho(
            trecho_id=t.id, ordem=t.ordem,
            parada_origem=t.parada_origem_codigo, parada_destino=t.parada_destino_codigo,
            atraso_origem_seg=origem, atraso_destino_seg=destino,
            variacao_seg=None if None in (origem, destino) else destino - origem,
            motivo=motivo_o or motivo_d,
            passagem_origem_id=ida.id if ida else None,
            passagem_destino_id=volta.id if volta else None,
        ))
    return PropagacaoViagem(
        viagem_observada_id=viagem.id, partida_programada_id=partida.id,
        importacao_id=importacao.id, versao_gtfs_id=importacao.versao_gtfs_id,
        data_operacao=importacao.data_operacao, versao_reconstrucao=viagem.versao_reconstrucao,
        versao_correspondencia=viagem.versao_correspondencia,
        versao_estimativa=passagens[0].versao_estimativa if passagens else None,
        trechos=tuple(resultado),
    )
