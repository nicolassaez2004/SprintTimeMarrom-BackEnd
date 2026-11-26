"""US08-T01: tempo de percurso e velocidade média por trecho (docs/US08-T01.md).

Cada amostra vem de duas ``PassagemEstimada`` (US15-T01) da mesma viagem, nas
paradas de origem e destino de um ``Trecho`` (US02-T02B):

- tempo = instante(destino) − instante(origem), em segundos;
- distância = medida(destino) − medida(origem) ao longo do percurso GTFS da
  versão, em metros (EPSG:31983), o mesmo referencial usado para interpolar as
  passagens; nunca a reta entre as paradas;
- velocidade = distância ÷ tempo × 3,6, em km/h.

Amostra inválida guarda o motivo e nunca vira velocidade zero. Sem passagem
nas duas paradas a amostra é ``SEM_DADOS``. Sem limites de plausibilidade
definidos pelo PO, a amostra que passa nas demais regras fica ``PENDENTE``.
Nada é gravado: brutos, passagens e trechos são apenas lidos.
"""

import math
from dataclasses import asdict, dataclass
from datetime import timedelta
from typing import Optional

from flask import current_app

from app import db
from app.correspondencia.geografia import PROJECAO
from app.models import (
    EstimativaPassagemViagem, ObservacaoTratada, PassagemEstimada, RegistroBrutoGPS, Trecho,
)
from app.viagens.passagens import Percurso

METODO = "DIFERENCA_PASSAGENS_MEDIDA_PERCURSO"
VERSAO = "us08-t01-v1"
UNIDADE = "km/h"
VALIDA, INVALIDA, PENDENTE, SEM_DADOS = "VALIDA", "INVALIDA", "PENDENTE", "SEM_DADOS"


@dataclass(frozen=True)
class Limites:
    """Regras do PO. ``None`` = ainda não definida (nunca um valor inventado)."""

    lacuna_maxima: Optional[timedelta] = None
    velocidade_minima_kmh: Optional[float] = None
    velocidade_maxima_kmh: Optional[float] = None

    def __post_init__(self):
        if self.lacuna_maxima is not None and self.lacuna_maxima <= timedelta(0):
            raise ValueError("lacuna máxima deve ser positiva")
        vmin, vmax = self.velocidade_minima_kmh, self.velocidade_maxima_kmh
        if (vmin is not None and vmin < 0) or (vmax is not None and vmax <= 0) or (
                None not in (vmin, vmax) and vmin >= vmax):
            raise ValueError("limites de velocidade inválidos")

    @property
    def versao(self):
        """Os limites entram na versão: trocar uma regra não reaproveita amostras."""
        fmt = lambda v: "indef" if v is None else f"{v:g}"  # noqa: E731
        lacuna = None if self.lacuna_maxima is None else self.lacuna_maxima.total_seconds()
        return (f"{VERSAO}-lacuna{fmt(lacuna)}-vmin{fmt(self.velocidade_minima_kmh)}"
                f"-vmax{fmt(self.velocidade_maxima_kmh)}")

    @classmethod
    def configurados(cls, lacuna_estimativa=None):
        """Config do app; lacuna sem valor próprio herda a da estimativa usada (US15)."""
        def numero(chave):
            valor = current_app.config.get(chave)
            return None if valor in (None, "") else float(valor)
        lacuna = numero("VELOCIDADE_LACUNA_MAXIMA_SEG")
        lacuna = lacuna if lacuna is not None else lacuna_estimativa
        return cls(None if lacuna is None else timedelta(seconds=lacuna),
                   numero("VELOCIDADE_MINIMA_KMH"), numero("VELOCIDADE_MAXIMA_KMH"))


@dataclass(frozen=True)
class AmostraVelocidade:
    trecho_id: str
    versao_gtfs_id: str
    linha_codigo: str
    sentido: str
    situacao: str
    motivo: Optional[str]
    viagem_observada_id: Optional[str] = None
    passagem_origem_id: Optional[str] = None
    passagem_destino_id: Optional[str] = None
    versao_estimativa: Optional[str] = None
    instante_origem: object = None
    instante_destino: object = None
    duracao_seg: Optional[float] = None
    distancia_m: Optional[float] = None
    velocidade_kmh: Optional[float] = None
    lacuna_maxima_observada_seg: Optional[float] = None
    unidade: str = UNIDADE
    metodo: str = METODO
    versao_calculo: str = VERSAO

    @property
    def valida(self):
        return self.situacao == VALIDA

    def como_dict(self):
        dados = asdict(self)
        for chave in ("instante_origem", "instante_destino"):
            dados[chave] = dados[chave] and dados[chave].isoformat()
        return dados


def geometria_valida(geometria):
    """Traçado com ao menos dois pontos distintos, finitos e em WGS84."""
    try:
        pontos = [(float(lon), float(lat)) for lon, lat in geometria or []]
    except (TypeError, ValueError):
        return False
    return len(set(pontos)) >= 2 and all(
        math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90
        and math.isfinite(sum(PROJECAO.transform(lon, lat))) for lon, lat in pontos)


def maior_intervalo(instantes):
    ordenados = sorted(set(instantes))
    return max((b - a for a, b in zip(ordenados, ordenados[1:])), default=None)


class AvaliadorVelocidade:
    def __init__(self, limites):
        self.limites = limites

    def avaliar(self, trecho, origem, destino, distancia_m, instantes_gps=()):
        """``instantes_gps``: horários das posições válidas do veículo (para a lacuna)."""
        base = dict(trecho_id=trecho.id, versao_gtfs_id=trecho.versao_gtfs_id,
                    linha_codigo=trecho.linha_codigo, sentido=trecho.sentido,
                    versao_calculo=self.limites.versao)
        if origem is None or destino is None:
            motivo = "passagem_origem_ausente" if origem is None else "passagem_destino_ausente"
            viagem = (origem or destino).viagem_observada_id if (origem or destino) else None
            return AmostraVelocidade(**base, situacao=SEM_DADOS, motivo=motivo,
                                     viagem_observada_id=viagem)

        duracao = (destino.instante - origem.instante).total_seconds()
        rastreio = dict(
            base, viagem_observada_id=origem.viagem_observada_id,
            passagem_origem_id=origem.id, passagem_destino_id=destino.id,
            versao_estimativa=origem.versao_estimativa,
            instante_origem=origem.instante, instante_destino=destino.instante, duracao_seg=duracao,
            distancia_m=distancia_m if distancia_m is not None and math.isfinite(distancia_m) else None)
        invalida = lambda motivo, **extra: AmostraVelocidade(  # noqa: E731
            **{**rastreio, **extra}, situacao=INVALIDA, motivo=motivo)

        if origem.viagem_observada_id != destino.viagem_observada_id:
            return invalida("viagens_diferentes", viagem_observada_id=None)
        if (origem.parada_codigo, destino.parada_codigo) != (
                trecho.parada_origem_codigo, trecho.parada_destino_codigo):
            return invalida("parada_incompativel")
        if {origem.versao_gtfs_id, destino.versao_gtfs_id} != {trecho.versao_gtfs_id} or (
                origem.versao_estimativa != destino.versao_estimativa):
            return invalida("versao_incompativel")
        importacao = origem.viagem.partida_programada.importacao
        if (importacao.versao_gtfs_id, importacao.linha_codigo, importacao.sentido) != (
                trecho.versao_gtfs_id, trecho.linha_codigo, trecho.sentido):
            return invalida("sentido_incompativel")
        if rastreio["distancia_m"] is None or distancia_m <= 0:
            return invalida("distancia_invalida")
        if duracao == 0:
            return invalida("duracao_nula")
        if duracao < 0:
            return invalida("ordem_temporal_invertida")

        # Lacuna: maior intervalo sem posição válida entre a primeira posição que
        # sustenta a origem e a última que sustenta o destino.
        inicio, fim = origem.posicao_anterior.horario_gps, destino.posicao_posterior.horario_gps
        lacuna = maior_intervalo([inicio, fim, *(t for t in instantes_gps if inicio <= t <= fim)])
        velocidade = distancia_m / duracao * 3.6
        rastreio.update(velocidade_kmh=velocidade, lacuna_maxima_observada_seg=lacuna.total_seconds())
        limites = self.limites
        if limites.lacuna_maxima is None:
            return AmostraVelocidade(**rastreio, situacao=PENDENTE, motivo="lacuna_maxima_indefinida")
        if lacuna > limites.lacuna_maxima:
            return invalida("lacuna_temporal")
        if limites.velocidade_minima_kmh is not None and velocidade < limites.velocidade_minima_kmh:
            return invalida("velocidade_abaixo_do_minimo")
        if limites.velocidade_maxima_kmh is not None and velocidade > limites.velocidade_maxima_kmh:
            return invalida("velocidade_acima_do_maximo")
        if None in (limites.velocidade_minima_kmh, limites.velocidade_maxima_kmh):
            return AmostraVelocidade(**rastreio, situacao=PENDENTE, motivo="plausibilidade_indefinida")
        return AmostraVelocidade(**rastreio, situacao=VALIDA, motivo=None)


def calcular_viagem(viagem, versao_estimativa, limites=None):
    """Uma amostra por trecho do percurso da viagem, na versão de estimativa dada.

    ``(amostras, motivo)``: ``motivo`` explica por que nenhum trecho pôde ser avaliado.
    """
    estimativa = db.session.get(EstimativaPassagemViagem, (viagem.id, versao_estimativa))
    if estimativa is None:
        return (), "estimativa_ausente"  # sem a US15-T01 não se afirma falta de passagem
    limites = limites or Limites.configurados(estimativa.lacuna_maxima_seg)
    importacao = viagem.partida_programada.importacao
    try:
        percurso, motivo = Percurso.da_rede(importacao.versao_gtfs_id, importacao.linha_codigo,
                                            importacao.sentido)
    except (ValueError, TypeError):
        percurso, motivo = None, "percurso_invalido"
    if percurso is None:
        return (), motivo
    trechos = Trecho.query.filter_by(versao_gtfs_id=percurso.versao_gtfs_id,
                                     linha_codigo=percurso.linha_codigo,
                                     sentido=percurso.sentido).order_by(Trecho.ordem).all()
    passagens = {p.parada_codigo: p for p in PassagemEstimada.query.filter_by(
        viagem_observada_id=viagem.id, versao_estimativa=versao_estimativa)}
    avaliador = AvaliadorVelocidade(limites)
    instantes = _instantes_validos(viagem, passagens.values())
    # Medida de cada parada no percurso: a mesma usada para interpolar as passagens.
    medidas = percurso.paradas
    amostras = []
    for trecho in trechos:
        distancia = None
        if geometria_valida(trecho.geometria):
            distancia = medidas[trecho.ordem][1] - medidas[trecho.ordem - 1][1]
        amostras.append(avaliador.avaliar(
            trecho, passagens.get(trecho.parada_origem_codigo),
            passagens.get(trecho.parada_destino_codigo), distancia, instantes))
    return tuple(amostras), None


def _instantes_validos(viagem, passagens):
    """Horários GPS das posições válidas do veículo no intervalo coberto pelas passagens."""
    posicoes = [pos for p in passagens for pos in (p.posicao_anterior, p.posicao_posterior)]
    if not posicoes:
        return ()
    prefixo = posicoes[0].prefixo_origem
    inicio, fim = min(p.horario_gps for p in posicoes), max(p.horario_gps for p in posicoes)
    return tuple(t for (t,) in db.session.query(RegistroBrutoGPS.horario_gps)
                 .join(ObservacaoTratada, ObservacaoTratada.bruto_id == RegistroBrutoGPS.id)
                 .filter(RegistroBrutoGPS.prefixo_origem == prefixo,
                         RegistroBrutoGPS.horario_gps.between(inicio, fim),
                         ObservacaoTratada.valida.is_(True),
                         ObservacaoTratada.versao_correspondencia == viagem.versao_correspondencia))
