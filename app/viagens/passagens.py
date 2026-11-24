"""Passagem estimada entre duas posições válidas da mesma viagem (US15-T01).

Regras (docs/US15-T01.md):

- O percurso é o trajeto GTFS versionado (US02-T02B) da linha/sentido da
  viagem, em metros (SIRGAS2000/UTM 23S, como na correspondência da US03).
- Cada posição e cada parada recebem uma medida: a distância ao longo do
  percurso até sua projeção no trecho mais próximo (empate: menor ordem).
- Só pares de posições consecutivas no tempo, ambas válidas, com lacuna
  dentro do limite e sem recuo de medida servem de base.
- Instante = t1 + (m_parada − m1) / (m2 − m1) × (t2 − t1); parada sobre a
  posição (m_parada = m1) recebe o próprio horário GPS.
- O avanço é monotônico: uma parada só é estimada além da maior medida já
  usada, e no máximo uma vez por viagem. Nada é inventado nas lacunas.
"""

from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from flask import current_app
from shapely.geometry import LineString

from app import db
from app.correspondencia.geografia import PROJECAO, Geografia
from app.models import PassagemEstimada, Trecho

METODO = "INTERPOLACAO_LINEAR_PERCURSO"
VERSAO = "us15-t01-v1"


class LimiteLacunaIndefinido(ValueError):
    """Sem lacuna máxima definida pelo PO nenhuma passagem pode ser estimada."""


@dataclass(frozen=True)
class Passagem:
    parada_codigo: str
    instante: object
    posicao_anterior_id: int
    posicao_posterior_id: int


@dataclass(frozen=True)
class ResultadoPassagens:
    passagens: tuple
    rejeicoes: tuple = ()           # (bruto anterior, bruto posterior, motivo)
    paradas_sem_evidencia: tuple = ()
    motivo: Optional[str] = None    # por que nada pôde ser avaliado


class Percurso:
    """Sequência de trechos de um único trajeto, com a medida de cada parada."""

    def __init__(self, trechos):
        trechos = sorted(trechos, key=lambda t: t.ordem)
        if not trechos or [t.ordem for t in trechos] != list(range(1, len(trechos) + 1)) or any(
            a.parada_destino_codigo != b.parada_origem_codigo for a, b in zip(trechos, trechos[1:])
        ):
            raise ValueError("trechos não formam uma sequência contínua de paradas")
        t0 = trechos[0]
        self.versao_gtfs_id, self.linha_codigo, self.sentido = t0.versao_gtfs_id, t0.linha_codigo, t0.sentido
        self.linhas, self.inicios, acumulado = [], [], 0.0
        for trecho in trechos:
            xs, ys = PROJECAO.transform(*zip(*trecho.geometria))
            linha = LineString(zip(xs, ys))
            self.linhas.append(linha)
            self.inicios.append(acumulado)
            acumulado += linha.length
        # Parada i é origem do trecho i e destino do trecho i-1.
        sequencia = [(t.origem, i) for i, t in enumerate(trechos)] + [(trechos[-1].destino, len(trechos))]
        self.paradas = tuple(
            (p.codigo, self.medida(p.longitude, p.latitude, {i - 1, i} & set(range(len(trechos)))))
            for p, i in sequencia
        )

    @classmethod
    def da_rede(cls, versao_gtfs_id, linha_codigo, sentido):
        """Percurso único da linha/sentido; ``(None, motivo)`` se não houver um só."""
        trechos = Trecho.query.filter_by(
            versao_gtfs_id=versao_gtfs_id, linha_codigo=linha_codigo, sentido=sentido
        ).all()
        trajetos = {t.trajeto for t in trechos}
        if not trajetos:
            return None, "percurso_inexistente"
        if len(trajetos) > 1:
            return None, "trajeto_ambiguo"
        return cls(trechos), None

    def medida(self, longitude, latitude, candidatos=None):
        ponto = Geografia.ponto(float(longitude), float(latitude))
        indices = sorted(candidatos) if candidatos is not None else range(len(self.linhas))
        _, i = min((self.linhas[i].distance(ponto), i) for i in indices)
        return self.inicios[i] + self.linhas[i].project(ponto)


class EstimadorPassagens:
    def __init__(self, lacuna_maxima):
        if lacuna_maxima is None:
            raise LimiteLacunaIndefinido(
                "lacuna máxima entre posições não definida (PASSAGEM_LACUNA_MAXIMA_SEG)")
        if lacuna_maxima <= timedelta(0):
            raise ValueError("lacuna_maxima deve ser positiva")
        self.lacuna_maxima = lacuna_maxima

    def estimar(self, percurso, observacoes):
        """``observacoes``: ``ObservacaoTratada`` de uma única viagem (com bruto)."""
        pontos = sorted((o for o in observacoes if o.bruto.horario_gps is not None),
                        key=lambda o: (o.bruto.horario_gps, o.bruto_id))
        self._conferir_viagem(percurso, pontos)
        codigos = tuple(codigo for codigo, _ in percurso.paradas)
        if sum(self._valida(o) for o in pontos) < 2:
            return ResultadoPassagens((), (), codigos, "posicoes_insuficientes")

        medidas = {o.bruto_id: percurso.medida(o.bruto.longitude, o.bruto.latitude)
                   for o in pontos if self._valida(o)}
        passagens, rejeicoes, frente = {}, [], float("-inf")
        for a, b in zip(pontos, pontos[1:]):
            motivo = self._motivo(a, b, medidas)
            if motivo:
                rejeicoes.append((a.bruto_id, b.bruto_id, motivo))
                continue
            m1, m2 = medidas[a.bruto_id], medidas[b.bruto_id]
            t1, t2 = a.bruto.horario_gps, b.bruto.horario_gps
            for codigo, m in percurso.paradas:
                if codigo not in passagens and m > frente and m1 <= m <= m2:
                    fracao = 0.0 if m2 == m1 else (m - m1) / (m2 - m1)
                    passagens[codigo] = Passagem(codigo, t1 + (t2 - t1) * fracao, a.bruto_id, b.bruto_id)
            frente = max(frente, m2)

        ordenadas = tuple(passagens[c] for c in dict.fromkeys(codigos) if c in passagens)
        faltantes = tuple(c for c in dict.fromkeys(codigos) if c not in passagens)
        return ResultadoPassagens(ordenadas, tuple(rejeicoes), faltantes)

    @staticmethod
    def _valida(observacao):
        b = observacao.bruto
        return observacao.valida is True and None not in (b.latitude, b.longitude)

    def _motivo(self, a, b, medidas):
        if not (self._valida(a) and self._valida(b)):
            return "posicao_invalida"
        intervalo = b.bruto.horario_gps - a.bruto.horario_gps
        if intervalo <= timedelta(0):
            return "ordem_temporal_incompativel"
        if intervalo > self.lacuna_maxima:
            return "lacuna_temporal"
        if medidas[b.bruto_id] < medidas[a.bruto_id]:
            return "movimento_contrario"
        return None

    def _conferir_viagem(self, percurso, pontos):
        if len({o.bruto.prefixo_origem for o in pontos}) > 1:
            raise ValueError("observações de veículos diferentes não formam uma viagem")
        esperado = (percurso.versao_gtfs_id, percurso.linha_codigo, percurso.sentido)
        if any((o.versao_gtfs_id, o.linha_codigo, o.sentido_gtfs) != esperado
               for o in pontos if o.valida):
            raise ValueError("observação válida de outra linha, sentido ou versão GTFS")


def lacuna_configurada():
    valor = current_app.config.get("PASSAGEM_LACUNA_MAXIMA_SEG")
    return None if valor in (None, "") else timedelta(seconds=float(valor))


def estimar_e_gravar(viagem, observacoes, lacuna_maxima=None):
    """Estima as passagens da ``ViagemObservada`` e grava as que faltarem.

    Idempotente: reexecutar com a mesma versão não duplica passagens. Não
    altera posições brutas nem observações tratadas; o commit fica com quem chama.
    """
    estimador = EstimadorPassagens(lacuna_maxima or lacuna_configurada())
    importacao = viagem.partida_programada.importacao
    percurso, motivo = Percurso.da_rede(importacao.versao_gtfs_id, importacao.linha_codigo,
                                        importacao.sentido)
    if percurso is None:
        return ResultadoPassagens((), motivo=motivo)
    fora = [o for o in observacoes if o.bruto.horario_gps is not None and not (
        viagem.saida_observada <= o.bruto.horario_gps
        and (viagem.fim_observado is None or o.bruto.horario_gps <= viagem.fim_observado))]
    if fora:
        raise ValueError("observação fora do intervalo observado da viagem")

    resultado = estimador.estimar(percurso, observacoes)
    existentes = {codigo for (codigo,) in db.session.query(PassagemEstimada.parada_codigo).filter_by(
        viagem_observada_id=viagem.id, versao_estimativa=VERSAO)}
    db.session.add_all(
        PassagemEstimada(
            viagem_observada_id=viagem.id, versao_gtfs_id=percurso.versao_gtfs_id,
            parada_codigo=p.parada_codigo, instante=p.instante, metodo=METODO,
            versao_estimativa=VERSAO, posicao_anterior_id=p.posicao_anterior_id,
            posicao_posterior_id=p.posicao_posterior_id,
        )
        for p in resultado.passagens if p.parada_codigo not in existentes
    )
    db.session.flush()
    return resultado
