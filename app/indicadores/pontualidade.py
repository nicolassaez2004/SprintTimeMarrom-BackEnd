"""Cálculo de diferenças e publicação da pontualidade (US16)."""

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256
import json
from typing import Iterable

from app import db
from app.models import IndicadorOperacional, PartidaProgramada
from app.viagens.separacao import ViagemObservada

VERSAO_ALGORITMO_PONTUALIDADE = "pontualidade-1.0"
LIMITE_ADIANTAMENTO_SEGUNDOS = 60
LIMITE_ATRASO_SEGUNDOS = 300


@dataclass(frozen=True)
class SaidaObservada:
    """Contexto explícito e saída confirmada pelo processamento anterior.

    ``viagem.inicio`` é o primeiro GPS, não uma confirmação de saída. Se essa
    confirmação não existe, ``saida_observada`` deve continuar None. O dia de
    operação e o pareamento também não são inferidos pelo horário civil.
    """

    viagem_id: str
    viagem: ViagemObservada
    data_operacao: date
    versao_gtfs_id: str
    saida_observada: datetime | None = None
    partida_programada_id: str | None = None


@dataclass(frozen=True)
class ReferenciaProgramacao:
    partida_id: str
    importacao_id: str
    linha_codigo: str
    sentido_gtfs: str
    data_operacao: date
    versao_gtfs_id: str
    tipo_oferta: str
    fonte: str
    versao_fonte: str
    fonte_url: str
    saida_prevista: datetime


@dataclass(frozen=True)
class DiferencaSaida:
    viagem_id: str
    data_operacao: date
    status: str
    motivo: str
    saida_observada: datetime | None
    programacao: ReferenciaProgramacao | None
    diferenca_segundos: float | None = None


@dataclass(frozen=True)
class ResultadoPontualidade:
    viagens: tuple[DiferencaSaida, ...]

    @property
    def contagens(self) -> dict[str, int]:
        contagens = Counter(item.status for item in self.viagens)
        return {status: contagens[status] for status in (
            "CALCULADA", "FREQUENCIA", "SEM_PAREAMENTO", "SAIDA_NAO_CONFIRMADA",
        )}

    @property
    def denominador(self) -> int:
        """Viagens fixas com saída confirmada e pareamento único; sujeito ao PO."""
        return self.contagens["CALCULADA"]


@dataclass(frozen=True)
class PercentualPontualidade:
    pontuais: int
    realizadas: int
    percentual: Decimal | None
    classificadas: int
    excluidas: dict[str, int]

    @property
    def dados_insuficientes(self) -> bool:
        return self.realizadas == 0


def calcular_percentual_pontualidade(
    diferencas: ResultadoPontualidade | Iterable[DiferencaSaida],
) -> PercentualPontualidade:
    """Conta saídas fixas confirmadas entre -60s e +300s, limites inclusivos.

    Somente itens CALCULADA entram no denominador. Frequência, pareamento
    ausente e saída não confirmada são excluídos e reportados separadamente.
    """
    viagens = diferencas.viagens if isinstance(diferencas, ResultadoPontualidade) else tuple(diferencas)
    contagens = Counter()
    pontuais = realizadas = 0
    for item in viagens:
        contagens[item.status] += 1
        if item.status != "CALCULADA":
            continue
        if item.diferenca_segundos is None:
            raise ValueError("viagem CALCULADA deve ter diferença de saída")
        realizadas += 1
        pontuais += (
            -LIMITE_ADIANTAMENTO_SEGUNDOS
            <= item.diferenca_segundos
            <= LIMITE_ATRASO_SEGUNDOS
        )
    percentual = None if realizadas == 0 else (
        Decimal(pontuais * 100) / Decimal(realizadas)
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return PercentualPontualidade(
        pontuais=pontuais,
        realizadas=realizadas,
        percentual=percentual,
        classificadas=len(viagens),
        excluidas={
            "frequencia": contagens["FREQUENCIA"],
            "sem_pareamento": contagens["SEM_PAREAMENTO"],
            "saida_nao_confirmada": contagens["SAIDA_NAO_CONFIRMADA"],
        },
    )


def publicar_pontualidade(
    resultado: ResultadoPontualidade,
    *, linha: str, sentido: str, inicio: datetime, fim: datetime,
    versao_gtfs_id: str, versao_correspondencia: str,
    versao_reconstrucao: str, versao_calculo: str = VERSAO_ALGORITMO_PONTUALIDADE,
) -> IndicadorOperacional:
    """Persiste o agregado versionado na tabela consumida por GET /indicadores."""
    if sentido not in ("0", "1"):
        raise ValueError("sentido deve ser '0' (ida) ou '1' (volta)")
    if not all(isinstance(valor, str) and valor.strip() for valor in (
        linha, versao_gtfs_id, versao_correspondencia, versao_reconstrucao, versao_calculo,
    )):
        raise ValueError("linha e versões GTFS, correspondência, reconstrução e cálculo são obrigatórias")
    inicio_utc, fim_utc = _utc(inicio, "inicio"), _utc(fim, "fim")
    if fim_utc <= inicio_utc:
        raise ValueError("fim deve ser posterior a inicio")

    agregado = calcular_percentual_pontualidade(resultado)
    versoes_programacao = sorted({
        (item.programacao.importacao_id, item.programacao.fonte,
         item.programacao.versao_fonte)
        for item in resultado.viagens if item.programacao is not None
    })
    identidade = {
        "tipo": "PONTUALIDADE_VIAGENS", "linha": linha, "sentido": sentido,
        "inicio": inicio_utc.isoformat(), "fim": fim_utc.isoformat(),
        "versao_gtfs_id": versao_gtfs_id,
        "versao_correspondencia": versao_correspondencia,
        "versao_reconstrucao": versao_reconstrucao,
        "versao_calculo": versao_calculo,
        "versoes_programacao": versoes_programacao,
    }
    chave = sha256(json.dumps(identidade, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    registro = IndicadorOperacional.query.filter_by(chave_resultado=chave).one_or_none()
    if registro is None:
        registro = IndicadorOperacional(chave_resultado=chave)
        db.session.add(registro)
    registro.versao_gtfs_id = versao_gtfs_id
    registro.linha_codigo = linha
    registro.sentido = sentido
    registro.parada_codigo = None
    registro.trecho_id = None
    registro.indicador = "PONTUALIDADE_VIAGENS"
    registro.valor = float(agregado.percentual) if agregado.percentual is not None else None
    registro.unidade = "%"
    registro.periodo_inicio = inicio_utc
    registro.periodo_fim = fim_utc
    registro.contagem_numerador = agregado.pontuais
    registro.contagem_denominador = agregado.realizadas
    registro.versao_conjunto = chave[:32]
    registro.versao_correspondencia = versao_correspondencia
    registro.versao_calculo = versao_calculo
    registro.qualidade = {
        "dados_insuficientes": agregado.dados_insuficientes,
        "motivo_insuficiencia": "NENHUMA_VIAGEM_FIXA_CLASSIFICAVEL" if agregado.dados_insuficientes else None,
        "viagens_classificadas": agregado.classificadas,
        "viagens_realizadas": agregado.realizadas,
        "viagens_pontuais": agregado.pontuais,
        "excluidas": agregado.excluidas,
        "limite_adiantamento_segundos": LIMITE_ADIANTAMENTO_SEGUNDOS,
        "limite_atraso_segundos": LIMITE_ATRASO_SEGUNDOS,
        "limites_inclusivos": True,
        "oferta_por_frequencia_excluida": True,
        "versao_reconstrucao": versao_reconstrucao,
        "capturas_programacao": [
            {"importacao_id": item[0], "fonte": item[1], "versao_fonte": item[2]}
            for item in versoes_programacao
        ],
    }
    db.session.commit()
    return registro


def _utc(instante: datetime, campo: str) -> datetime:
    if not isinstance(instante, datetime) or instante.utcoffset() is None:
        raise ValueError(f"{campo} deve ser datetime com fuso explícito")
    return instante.astimezone(timezone.utc)


def _contexto(linha, sentido, dia, versao):
    if not isinstance(linha, str) or not linha.strip():
        raise ValueError("linha_codigo deve ser informado")
    if sentido not in ("0", "1"):
        raise ValueError("sentido_gtfs deve ser 0 ou 1; não usar códigos Olho Vivo")
    if isinstance(dia, datetime) or not isinstance(dia, date):
        raise ValueError("data_operacao deve ser date explícita")
    if not isinstance(versao, str) or not versao.strip():
        raise ValueError("versao_gtfs_id deve ser informada")
    return linha, sentido, dia, versao


def _referencia(partida: PartidaProgramada) -> ReferenciaProgramacao:
    if not partida.id or partida.importacao is None or not partida.importacao.id:
        raise ValueError("partida deve ter ID e importação persistidos")
    lote = partida.importacao
    _contexto(lote.linha_codigo, lote.sentido, lote.data_operacao, lote.versao_gtfs_id)
    if partida.tipo_oferta not in ("FIXA", "FREQUENCIA"):
        raise ValueError("tipo_oferta desconhecido")
    if lote.fonte not in ("OFICIAL", "GTFS") or not lote.versao_fonte or not lote.fonte_url:
        raise ValueError("programação deve preservar fonte, versão e referência")
    return ReferenciaProgramacao(
        partida.id, lote.id, lote.linha_codigo, lote.sentido, lote.data_operacao,
        lote.versao_gtfs_id, partida.tipo_oferta, lote.fonte, lote.versao_fonte,
        lote.fonte_url, _utc(partida.saida_prevista, "saida_prevista"),
    )


def calcular_diferencas(
    saidas: Iterable[SaidaObservada], partidas: Iterable[PartidaProgramada],
) -> ResultadoPontualidade:
    """Compara saídas confirmadas com partidas já selecionadas da US04.

    O chamador seleciona uma única importação por linha/sentido/dia/versão,
    respeitando a prioridade oficial e o calendário da US04. O vínculo por ID
    é explícito e um para um; horários próximos não criam pareamento.
    Ofertas por frequência nunca recebem diferença nem classificação.
    O resultado mantém a ordem recebida e não escreve no banco.
    """
    referencias = {}
    somente_frequencia_por_contexto = {}
    importacoes = {}
    for partida in partidas:
        ref = _referencia(partida)
        if ref.partida_id in referencias:
            raise ValueError("partida repetida na programação recebida")
        chave = _contexto(ref.linha_codigo, ref.sentido_gtfs,
                          ref.data_operacao, ref.versao_gtfs_id)
        if chave in importacoes and importacoes[chave] != ref.importacao_id:
            raise ValueError("programação ambígua: selecione uma importação por contexto")
        importacoes[chave] = ref.importacao_id
        referencias[ref.partida_id] = ref
        somente_frequencia_por_contexto[chave] = (
            somente_frequencia_por_contexto.get(chave, True)
            and ref.tipo_oferta == "FREQUENCIA"
        )

    entradas = list(saidas)
    ids = set()
    contextos = []
    instantes = []
    for entrada in entradas:
        if not isinstance(entrada.viagem_id, str) or not entrada.viagem_id.strip():
            raise ValueError("viagem_id deve ser informado")
        if entrada.viagem_id in ids:
            raise ValueError("viagem_id repetido no conjunto observado")
        ids.add(entrada.viagem_id)
        if entrada.partida_programada_id is not None and (
            not isinstance(entrada.partida_programada_id, str)
            or not entrada.partida_programada_id.strip()
        ):
            raise ValueError("partida_programada_id deve ser ID ou None")
        viagem = entrada.viagem
        contextos.append(_contexto(viagem.linha_codigo, viagem.sentido_gtfs,
                                   entrada.data_operacao, entrada.versao_gtfs_id))
        inicio = _utc(viagem.inicio, "viagem.inicio")
        fim = _utc(viagem.fim, "viagem.fim")
        if fim < inicio:
            raise ValueError("viagem.fim não pode anteceder viagem.inicio")
        instantes.append(None if entrada.saida_observada is None else
                         _utc(entrada.saida_observada, "saida_observada"))

    usos = Counter(e.partida_programada_id for e in entradas
                   if e.partida_programada_id is not None)
    resultados = []
    for entrada, contexto, instante in zip(entradas, contextos, instantes):
        ref = referencias.get(entrada.partida_programada_id)
        diferenca = None
        if ref is None:
            if (
                entrada.partida_programada_id is None
                and somente_frequencia_por_contexto.get(contexto, False)
            ):
                status, motivo = "FREQUENCIA", "OFERTA_POR_FREQUENCIA"
            else:
                status = "SEM_PAREAMENTO"
                motivo = ("PAREAMENTO_NAO_INFORMADO" if entrada.partida_programada_id is None
                          else "PARTIDA_NAO_ENCONTRADA")
        elif contexto != (ref.linha_codigo, ref.sentido_gtfs,
                           ref.data_operacao, ref.versao_gtfs_id):
            status, motivo = "SEM_PAREAMENTO", "CONTEXTO_DIVERGENTE"
        elif ref.tipo_oferta == "FREQUENCIA":
            status, motivo = "FREQUENCIA", "OFERTA_POR_FREQUENCIA"
        elif usos[ref.partida_id] > 1:
            status, motivo = "SEM_PAREAMENTO", "PARTIDA_COMPARTILHADA"
        elif instante is None:
            status, motivo = "SAIDA_NAO_CONFIRMADA", "PRIMEIRO_GPS_NAO_CONFIRMA_SAIDA"
        else:
            status, motivo = "CALCULADA", "FIXA_PAREADA"
            diferenca = (instante - ref.saida_prevista).total_seconds()
        resultados.append(DiferencaSaida(
            entrada.viagem_id, entrada.data_operacao, status, motivo,
            instante, ref, diferenca,
        ))
    return ResultadoPontualidade(tuple(resultados))
