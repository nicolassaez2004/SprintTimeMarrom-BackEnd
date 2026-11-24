"""US16-T01: diferença por viagem, sem inferir saídas ou publicar percentuais."""

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Iterable

from app.models import PartidaProgramada
from app.viagens.separacao import ViagemObservada


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
    por_contexto = defaultdict(list)
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
        por_contexto[chave].append(ref)

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
            ofertas = por_contexto.get(contexto, ())
            if entrada.partida_programada_id is None and ofertas and all(
                item.tipo_oferta == "FREQUENCIA" for item in ofertas
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
