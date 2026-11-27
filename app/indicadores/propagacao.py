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
- ``publicar_propagacao`` grava uma linha de ``IndicadorOperacional`` por
  trecho da viagem (``VARIACAO_ATRASO_TRECHO``), com valor NULL e motivo quando
  não há variação; o período é o dia operacional em São Paulo.

US19-T03 (docs/US19-T03.md): ``agregar_mes`` resume essas linhas por trecho no
mês (``AUMENTO_ATRASO_TRECHO_MENSAL``) com a métrica escolhida pelo PO; o
ranking só lê esse resultado (``app.consultas.atrasos``).

US19-T02 (docs/US19-T02.md), no fim do módulo: atraso de chegada de uma viagem
(último trecho da T01) × atraso de partida da viagem seguinte do mesmo veículo
(diferença de saída da US16), com pares descartados e motivo.
"""

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from hashlib import sha256
import json
import re
import statistics
from typing import Optional

from flask import current_app
from sqlalchemy.orm import aliased

from app import db
from app.core.tempo import FUSO
from app.indicadores.cumprimento import gravar_indicador
from app.indicadores.pontualidade import VERSAO_ALGORITMO_PONTUALIDADE, diferenca_saida
from app.models import (
    ImportacaoProgramacao, IndicadorOperacional, PassagemEstimada, RegistroBrutoGPS,
)
from app.qualidade.service import FalhasColeta
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
    motivo_origem: Optional[str] = None
    motivo_destino: Optional[str] = None
    passagem_origem_id: Optional[str] = None
    passagem_destino_id: Optional[str] = None

    @property
    def motivo(self):
        """Motivo de ``variacao_seg`` ser ``None`` (o da origem, se as duas pontas faltarem)."""
        return self.motivo_origem or self.motivo_destino


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
            motivo_origem=motivo_o, motivo_destino=motivo_d,
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


# --- US19-T02: chegada de uma viagem × partida seguinte do mesmo veículo ----------

VERSAO_CORRELACAO = "correlacao-chegada-partida-1.0"
INDICADOR_CORRELACAO = "CORRELACAO_ATRASO_CHEGADA_PARTIDA"
UNIDADE_CORRELACAO = "r (adimensional)"
# Proposta ao PO, não regra aprovada: Pearson e piso técnico de 3 pares (com 2
# pontos Pearson é sempre ±1 ou indefinido).
METODO_CORRELACAO = "pearson"
AMOSTRA_MINIMA = 3


class IntervaloMaximoIndefinido(ValueError):
    """Sem o intervalo máximo chegada → partida (decisão do PO) nenhum par é formado."""


def intervalo_configurado():
    valor = current_app.config.get("PROPAGACAO_INTERVALO_MAXIMO_SEG")
    return None if valor in (None, "") else timedelta(seconds=float(valor))


@dataclass(frozen=True)
class ViagemVeiculo:
    """Fatos de uma ``ViagemObservada`` para o pareamento, todos rastreáveis.

    ``prefixos``: prefixos dos GPS brutos que sustentam as passagens da viagem.
    ``chegada``: passagem estimada na última parada; ``termino`` cai para
    ``fim_observado`` quando ela não existe (só para detectar sobreposição).
    """
    viagem_id: str
    partida_programada_id: str
    prefixos: frozenset
    linha_codigo: str
    sentido: str
    versao_gtfs_id: str
    data_operacao: object
    saida: datetime
    atraso_partida_seg: Optional[float]
    motivo_partida: Optional[str]
    chegada: Optional[datetime]
    atraso_chegada_seg: Optional[float]
    motivo_chegada: Optional[str]
    termino: Optional[datetime]
    versao_reconstrucao: str
    versao_correspondencia: str
    versao_estimativa: Optional[str]


def _utc(instante):
    return None if instante is None else instante.astimezone(timezone.utc)


def viagem_do_veiculo(viagem, propagacao):
    """``ViagemObservada`` + seu ``PropagacaoViagem`` (T01) → ``ViagemVeiculo``. Só lê."""
    if (propagacao.viagem_observada_id, propagacao.partida_programada_id) != (
            viagem.id, viagem.partida_programada_id):
        raise ValueError("resultado da US19-T01 de outra viagem")
    partida = viagem.partida_programada
    saida = diferenca_saida(viagem, partida)
    ultimo = propagacao.trechos[-1]
    passagem = (db.session.get(PassagemEstimada, ultimo.passagem_destino_id)
                if ultimo.passagem_destino_id else None)
    anterior, posterior = aliased(RegistroBrutoGPS), aliased(RegistroBrutoGPS)
    prefixos = frozenset(p for linha in (
        db.session.query(anterior.prefixo_origem, posterior.prefixo_origem)
        .select_from(PassagemEstimada)
        .join(anterior, anterior.id == PassagemEstimada.posicao_anterior_id)
        .join(posterior, posterior.id == PassagemEstimada.posicao_posterior_id)
        .filter(PassagemEstimada.viagem_observada_id == viagem.id,
                PassagemEstimada.versao_estimativa == propagacao.versao_estimativa)
    ) for p in linha)
    chegada = _utc(passagem.instante) if passagem else None
    return ViagemVeiculo(
        viagem_id=viagem.id, partida_programada_id=partida.id, prefixos=prefixos,
        linha_codigo=partida.importacao.linha_codigo, sentido=partida.importacao.sentido,
        versao_gtfs_id=propagacao.versao_gtfs_id, data_operacao=propagacao.data_operacao,
        saida=saida.saida_observada, atraso_partida_seg=saida.diferenca_segundos,
        motivo_partida=None if saida.diferenca_segundos is not None else saida.motivo,
        chegada=chegada, atraso_chegada_seg=ultimo.atraso_destino_seg,
        motivo_chegada=ultimo.motivo_destino,
        termino=chegada or _utc(viagem.fim_observado),
        versao_reconstrucao=viagem.versao_reconstrucao,
        versao_correspondencia=viagem.versao_correspondencia,
        versao_estimativa=propagacao.versao_estimativa,
    )


@dataclass(frozen=True)
class ParViagens:
    prefixo: str
    anterior: ViagemVeiculo
    seguinte: ViagemVeiculo
    motivo: Optional[str] = None

    @property
    def intervalo_seg(self):
        a = self.anterior
        return None if a.chegada is None else (self.seguinte.saida - a.chegada).total_seconds()

    def registro(self):
        a, b = self.anterior, self.seguinte
        detalhe = (a.motivo_chegada if self.motivo == "chegada_sem_atraso"
                   else b.motivo_partida if self.motivo == "partida_sem_atraso" else None)
        return {
            "prefixo": self.prefixo, "motivo": self.motivo, "detalhe": detalhe,
            "viagem_chegada": a.viagem_id, "viagem_partida": b.viagem_id,
            "partida_programada_chegada": a.partida_programada_id,
            "partida_programada_partida": b.partida_programada_id,
            "data_operacao_chegada": a.data_operacao.isoformat(),
            "data_operacao_partida": b.data_operacao.isoformat(),
            "chegada": a.chegada.isoformat() if a.chegada else None,
            "saida": b.saida.isoformat(), "intervalo_seg": self.intervalo_seg,
            "atraso_chegada_seg": a.atraso_chegada_seg, "atraso_partida_seg": b.atraso_partida_seg,
        }


def _motivo_par(a, b, intervalo_maximo):
    """Primeiro motivo que impede usar (a, b); ``None`` = par válido."""
    if len(a.prefixos) > 1 or len(b.prefixos) > 1:
        return "troca_de_veiculo"
    if a.chegada is not None and a.chegada < a.saida:
        return "horario_inconsistente"
    if b.saida == a.saida or (a.termino is not None and b.saida < a.termino):
        return "viagens_sobrepostas"
    if a.atraso_chegada_seg is None:
        return "chegada_sem_atraso"
    if b.atraso_partida_seg is None:
        return "partida_sem_atraso"
    if b.saida - a.chegada > intervalo_maximo:
        return "lacuna_excessiva"
    if a.versao_gtfs_id != b.versao_gtfs_id:
        return "versao_gtfs_diferente"
    if a.linha_codigo != b.linha_codigo:
        return "linha_diferente"
    return None


@dataclass(frozen=True)
class ResultadoCorrelacao:
    pares: tuple
    descartados: tuple
    excluidas: tuple
    coeficiente: Optional[float]
    motivo_sem_valor: Optional[str]
    intervalo_maximo: timedelta
    versoes: tuple

    @property
    def amostra(self):
        return len(self.pares)


def pearson(x, y):
    """``(r, None)`` ou ``(None, motivo)``; nunca transforma indefinido em zero."""
    if len(x) < AMOSTRA_MINIMA:
        return None, "amostra_insuficiente"
    if len(set(x)) == 1 or len(set(y)) == 1:
        return None, "variancia_zero"
    return statistics.correlation(x, y), None


def correlacionar_chegada_partida(viagens, intervalo_maximo=None):
    """Pareia cada viagem com a imediatamente seguinte do mesmo prefixo e correlaciona.

    Viagens de um veículo são ordenadas por saída observada; só o vizinho
    imediato é candidato (nunca "qualquer partida futura"). Uma viagem com mais
    de um prefixo entra na sequência de cada um, para que seus pares sejam
    descartados como troca de veículo em vez de serem pulados. Não grava nada.
    """
    if intervalo_maximo is None:
        intervalo_maximo = intervalo_configurado()
    if intervalo_maximo is None:
        raise IntervaloMaximoIndefinido(
            "intervalo máximo entre chegada e próxima partida não definido "
            "(PROPAGACAO_INTERVALO_MAXIMO_SEG)")
    if intervalo_maximo <= timedelta(0):
        raise ValueError("intervalo_maximo deve ser positivo")
    viagens = list(viagens)
    if len({v.viagem_id for v in viagens}) != len(viagens):
        raise ValueError("viagem repetida")
    if len({(v.versao_reconstrucao, v.versao_correspondencia) for v in viagens}) > 1:
        raise ValueError("viagens de versões de reconstrução ou correspondência diferentes")
    estimativas = {v.versao_estimativa for v in viagens} - {None}
    if len(estimativas) > 1:
        raise ValueError("viagens de versões de estimativa diferentes")

    excluidas, sequencias = [], defaultdict(list)
    for v in sorted(viagens, key=lambda v: v.viagem_id):
        if not v.prefixos or None in v.prefixos:
            excluidas.append((v.viagem_id, "veiculo_nao_identificado"))
        for prefixo in v.prefixos - {None}:
            sequencias[prefixo].append(v)
    pares, descartados, vistos = [], [], set()
    for prefixo in sorted(sequencias):
        seq = sorted(sequencias[prefixo], key=lambda v: (v.saida, v.viagem_id))
        for a, b in zip(seq, seq[1:]):
            if (a.viagem_id, b.viagem_id) in vistos:
                continue
            vistos.add((a.viagem_id, b.viagem_id))
            par = ParViagens(prefixo, a, b, _motivo_par(a, b, intervalo_maximo))
            (descartados if par.motivo else pares).append(par)
    coeficiente, motivo = pearson([p.anterior.atraso_chegada_seg for p in pares],
                                  [p.seguinte.atraso_partida_seg for p in pares])
    primeira = viagens[0] if viagens else None
    versoes = (primeira.versao_reconstrucao, primeira.versao_correspondencia,
               next(iter(estimativas), None)) if primeira else (None, None, None)
    return ResultadoCorrelacao(tuple(pares), tuple(descartados), tuple(excluidas),
                               coeficiente, motivo, intervalo_maximo, versoes)


def recortar_correlacao(resultado, *, linha, inicio, fim, versao_gtfs_id):
    """Pares da linha/rede com chegada (ou saída, sem chegada) em ``[inicio, fim)``; r recalculado.

    Pareie todas as viagens do período maior (para manter o vizinho imediato
    correto nas bordas) e recorte cada escala — 15 min, faixa, dia ou mês — aqui,
    em vez de filtrar viagens antes do pareamento. ``descartados_fora_do_escopo``
    preserva quantos descartes ficaram fora do recorte.
    """
    if inicio.utcoffset() is None or fim.utcoffset() is None or fim <= inicio:
        raise ValueError("inicio e fim com fuso, fim posterior a inicio")
    inicio, fim = _utc(inicio), _utc(fim)

    def no_escopo(par):
        a = par.anterior
        return ((a.linha_codigo, a.versao_gtfs_id) == (linha, versao_gtfs_id)
                and inicio <= (a.chegada or a.saida) < fim)

    pares = tuple(p for p in resultado.pares if no_escopo(p))
    coeficiente, motivo = pearson([p.anterior.atraso_chegada_seg for p in pares],
                                  [p.seguinte.atraso_partida_seg for p in pares])
    return ResultadoCorrelacao(pares, resultado.descartados, resultado.excluidas, coeficiente, motivo,
                               resultado.intervalo_maximo, resultado.versoes)


def montar_correlacao(resultado, *, linha, inicio, fim, versao_gtfs_id):
    """Colunas de ``IndicadorOperacional`` para os pares cuja chegada é da linha e janela.

    O chamador escolhe as viagens. Par válido fora da linha, versão GTFS ou
    período ``[inicio, fim)`` (pela chegada) é erro: o coeficiente já o usou.
    Descartados fora do escopo (pela chegada ou, sem ela, pela saída) não entram
    no denominador e são contados em ``descartados_fora_do_escopo``.
    """
    if inicio.utcoffset() is None or fim.utcoffset() is None or fim <= inicio:
        raise ValueError("inicio e fim com fuso, fim posterior a inicio")
    inicio, fim = _utc(inicio), _utc(fim)

    def no_escopo(par):
        a = par.anterior
        return ((a.linha_codigo, a.versao_gtfs_id) == (linha, versao_gtfs_id)
                and inicio <= (a.chegada or a.saida) < fim)

    if not all(no_escopo(p) for p in resultado.pares):
        raise ValueError("par válido fora da linha, versão GTFS ou período publicado")
    descartados = [p for p in resultado.descartados if no_escopo(p)]
    recon, corresp, estimativa = resultado.versoes
    parametros = {"metodo": METODO_CORRELACAO, "amostra_minima": AMOSTRA_MINIMA,
                  "intervalo_maximo_seg": resultado.intervalo_maximo.total_seconds()}
    identidade = {"tipo": INDICADOR_CORRELACAO, "linha": linha, "inicio": inicio.isoformat(),
                  "fim": fim.isoformat(), "versao_gtfs_id": versao_gtfs_id,
                  "versoes": [recon, corresp, estimativa, VERSAO_CALCULO,
                              VERSAO_ALGORITMO_PONTUALIDADE, VERSAO_CORRELACAO], **parametros}
    chave = sha256(json.dumps(identidade, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {
        "chave_resultado": chave, "versao_gtfs_id": versao_gtfs_id, "linha_codigo": linha,
        "sentido": None, "parada_codigo": None, "trecho_id": None,
        "indicador": INDICADOR_CORRELACAO, "valor": resultado.coeficiente,
        "unidade": UNIDADE_CORRELACAO, "periodo_inicio": inicio, "periodo_fim": fim,
        "contagem_numerador": resultado.amostra,
        "contagem_denominador": resultado.amostra + len(descartados),
        "versao_conjunto": chave[:32], "versao_correspondencia": corresp,
        "versao_calculo": VERSAO_CORRELACAO,
        "qualidade": {
            **parametros, "situacao_metodo": "PROPOSTA_PENDENTE_PO",
            "motivo_sem_valor": resultado.motivo_sem_valor,
            "versao_reconstrucao": recon, "versao_estimativa": estimativa,
            "versao_atraso_chegada": VERSAO_CALCULO,
            "versao_atraso_partida": VERSAO_ALGORITMO_PONTUALIDADE,
            "descartes_por_motivo": dict(sorted(Counter(p.motivo for p in descartados).items())),
            "descartados_fora_do_escopo": len(resultado.descartados) - len(descartados),
            "pares_validos": [p.registro() for p in resultado.pares],
            "pares_descartados": [p.registro() for p in descartados],
            "viagens_excluidas": [{"viagem": v, "motivo": m} for v, m in resultado.excluidas],
        },
    }


def publicar_correlacao(resultado, **contexto):
    """Upsert idempotente pela chave (reprocessar não duplica)."""
    return gravar_indicador(montar_correlacao(resultado, **contexto))


# --- US19-T01: persistência por viagem e trecho ------------------------------------

INDICADOR_VARIACAO = "VARIACAO_ATRASO_TRECHO"


def _chave(identidade):
    return sha256(json.dumps(identidade, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def dia_operacional(data_operacao):
    """``[00:00, 24:00)`` do dia operacional em São Paulo, em UTC."""
    inicio = datetime.combine(data_operacao, time(), FUSO)
    return _utc(inicio), _utc(inicio + timedelta(days=1))


def publicar_propagacao(propagacao, *, falhas=None, commit=True):
    """Uma linha por trecho do percurso; reprocessar a mesma viagem/versões não duplica.

    ``valor`` = variação (s) ou NULL com ``qualidade.motivo``; nunca zero inventado.
    """
    importacao = db.session.get(ImportacaoProgramacao, propagacao.importacao_id)
    inicio, fim = dia_operacional(propagacao.data_operacao)
    falhas = falhas or FalhasColeta()
    registros = []
    for t in propagacao.trechos:
        chave = _chave({"tipo": INDICADOR_VARIACAO, "viagem": propagacao.viagem_observada_id,
                        "trecho": t.trecho_id, "versao_calculo": propagacao.versao_calculo,
                        "versao_estimativa": propagacao.versao_estimativa})
        registros.append(gravar_indicador({
            "chave_resultado": chave, "versao_gtfs_id": propagacao.versao_gtfs_id,
            "linha_codigo": importacao.linha_codigo, "sentido": importacao.sentido,
            "parada_codigo": None, "trecho_id": t.trecho_id, "indicador": INDICADOR_VARIACAO,
            "valor": t.variacao_seg, "unidade": propagacao.unidade,
            "periodo_inicio": inicio, "periodo_fim": fim,
            "contagem_numerador": None, "contagem_denominador": None,
            "versao_conjunto": chave[:32], "versao_correspondencia": propagacao.versao_correspondencia,
            "versao_calculo": propagacao.versao_calculo,
            "qualidade": {
                "viagem_observada_id": propagacao.viagem_observada_id,
                "partida_programada_id": propagacao.partida_programada_id,
                "importacao_id": propagacao.importacao_id,
                "data_operacao": propagacao.data_operacao.isoformat(),
                "ordem": t.ordem, "parada_origem": t.parada_origem, "parada_destino": t.parada_destino,
                "atraso_origem_seg": t.atraso_origem_seg, "atraso_destino_seg": t.atraso_destino_seg,
                "motivo": t.motivo, "motivo_origem": t.motivo_origem, "motivo_destino": t.motivo_destino,
                "passagem_origem_id": t.passagem_origem_id, "passagem_destino_id": t.passagem_destino_id,
                "versao_reconstrucao": propagacao.versao_reconstrucao,
                "versao_estimativa": propagacao.versao_estimativa,
            },
        }, commit=False, falhas=falhas))
    if commit:
        db.session.commit()
    return registros


# --- US19-T03: agregação mensal por trecho (base do ranking) -----------------------

INDICADOR_MENSAL = "AUMENTO_ATRASO_TRECHO_MENSAL"
VERSAO_MENSAL = "aumento-atraso-mensal-1.0"
# Candidatas; nenhuma é padrão. A escolha é do PO (PROPAGACAO_RANKING_METRICA).
METRICAS_MENSAIS = {"MEDIA": statistics.fmean, "MEDIANA": statistics.median}


class MetricaMensalIndefinida(ValueError):
    """Sem a métrica mensal aprovada pelo PO não há agregado nem ranking."""


def metrica_configurada():
    valor = (current_app.config.get("PROPAGACAO_RANKING_METRICA") or "").strip().upper()
    if valor and valor not in METRICAS_MENSAIS:
        raise ValueError(f"PROPAGACAO_RANKING_METRICA deve ser uma de {sorted(METRICAS_MENSAIS)}")
    return valor or None


def versao_mensal(metrica):
    return f"{VERSAO_MENSAL}:{metrica}"


def limites_mes(mes):
    """``'AAAA-MM'`` → ``[1º dia 00:00, 1º dia do mês seguinte 00:00)`` de São Paulo, em UTC."""
    achado = re.fullmatch(r"(\d{4})-(\d{2})", mes or "")
    if not achado or not 1 <= int(achado[2]) <= 12 or int(achado[1]) < 1:
        raise ValueError("mes deve estar no formato AAAA-MM")
    ano, numero = int(achado[1]), int(achado[2])
    inicio = datetime(ano, numero, 1, tzinfo=FUSO)
    fim = datetime(ano + numero // 12, numero % 12 + 1, 1, tzinfo=FUSO)
    return _utc(inicio), _utc(fim)


def _versoes_entrada(registro):
    q = registro.qualidade or {}
    return (registro.versao_gtfs_id, registro.versao_calculo, registro.versao_correspondencia,
            q.get("versao_reconstrucao"), q.get("versao_estimativa"))


def agregar_mes(mes, metrica=None, *, commit=True):
    """Resume as variações de cada trecho cujo dia operacional cai no mês.

    Um registro por trecho/mês/métrica (reprocessar substitui). ``valor`` NULL
    quando o trecho não tem variação calculável ou mistura versões de entrada.
    """
    metrica = metrica or metrica_configurada()
    if metrica is None:
        raise MetricaMensalIndefinida(
            "métrica mensal do aumento de atraso não definida (PROPAGACAO_RANKING_METRICA)")
    if metrica not in METRICAS_MENSAIS:
        raise ValueError(f"metrica deve ser uma de {sorted(METRICAS_MENSAIS)}")
    inicio, fim = limites_mes(mes)
    por_trecho = defaultdict(list)
    for registro in (IndicadorOperacional.query
                     .filter(IndicadorOperacional.indicador == INDICADOR_VARIACAO,
                             IndicadorOperacional.periodo_inicio >= inicio,
                             IndicadorOperacional.periodo_inicio < fim)
                     .order_by(IndicadorOperacional.trecho_id, IndicadorOperacional.id)):
        por_trecho[registro.trecho_id].append(registro)
    falhas, publicados = FalhasColeta(), []
    for trecho_id, registros in sorted(por_trecho.items()):
        versoes = sorted({_versoes_entrada(r) for r in registros}, key=str)
        validos = [r.valor for r in registros if r.valor is not None]
        if len(versoes) > 1:
            valor, motivo = None, "versoes_misturadas"
        elif not validos:
            valor, motivo = None, "sem_variacao_calculavel"
        else:
            valor, motivo = float(METRICAS_MENSAIS[metrica](validos)), None
        ref = registros[0]
        chave = _chave({"tipo": INDICADOR_MENSAL, "trecho": trecho_id, "mes": mes,
                        "versao_calculo": versao_mensal(metrica)})
        publicados.append(gravar_indicador({
            "chave_resultado": chave, "versao_gtfs_id": ref.versao_gtfs_id,
            "linha_codigo": ref.linha_codigo, "sentido": ref.sentido, "parada_codigo": None,
            "trecho_id": trecho_id, "indicador": INDICADOR_MENSAL, "valor": valor, "unidade": UNIDADE,
            "periodo_inicio": inicio, "periodo_fim": fim,
            "contagem_numerador": len(validos), "contagem_denominador": len(registros),
            "versao_conjunto": chave[:32], "versao_correspondencia": ref.versao_correspondencia,
            "versao_calculo": versao_mensal(metrica),
            "qualidade": {
                "mes": mes, "fuso": "America/Sao_Paulo", "criterio_mes": "data operacional",
                "metrica": metrica, "metrica_origem": "PROPAGACAO_RANKING_METRICA (decisão do PO)",
                "motivo_sem_valor": motivo, "amostras_validas": len(validos),
                "amostras_total": len(registros), "cobertura": len(validos) / len(registros),
                "sem_valor_por_motivo": dict(sorted(Counter(
                    (r.qualidade or {}).get("motivo") or "sem_motivo"
                    for r in registros if r.valor is None).items())),
                "viagens": len({(r.qualidade or {}).get("viagem_observada_id") for r in registros}),
                "versoes_entrada": [dict(zip(("versao_gtfs_id", "versao_calculo", "versao_correspondencia",
                                              "versao_reconstrucao", "versao_estimativa"), v))
                                    for v in versoes],
            },
        }, commit=False, falhas=falhas))
    if commit:
        db.session.commit()
    return publicados
