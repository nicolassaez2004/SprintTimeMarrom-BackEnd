"""US14: calculation and publication of completed scheduled trips."""

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256
import json

from app import db
from app.models import (
    ImportacaoProgramacao,
    IndicadorOperacional,
    PartidaProgramada,
    ViagemObservada,
)

COBERTURA_MINIMA = 0.80
TOLERANCIA_MAXIMA_SEGUNDOS = 15 * 60
VERSAO_ALGORITMO = "cumprimento-1.0"


@dataclass(frozen=True)
class ResultadoCumprimento:
    cumpridas: int
    programadas: int
    percentual: Decimal | None


def _tolerancia_saida(partida, programadas):
    """Half the headway; fixed schedules use their nearest positive neighbor.

    For a fixed schedule, only departures in the same imported timetable count.
    At the first/last departure the single available neighbor is used; if the
    timetable has no distinct neighbor, no interval can be inferred.
    """
    if partida.tipo_oferta == "FREQUENCIA":
        intervalo = partida.intervalo_seg
    else:
        mesma_grade = sorted(
            (p for p in programadas
             if p.importacao_id == partida.importacao_id
             and p.tipo_oferta == "FIXA"),
            key=lambda p: (p.saida_prevista, p.ordem, p.id),
        )
        posicoes = [i for i, item in enumerate(mesma_grade) if item.id == partida.id]
        if not posicoes:
            return None
        indice = posicoes[0]
        vizinhos = []
        for outro in (mesma_grade[indice - 1] if indice > 0 else None,
                      mesma_grade[indice + 1] if indice + 1 < len(mesma_grade) else None):
            if outro is None:
                continue
            diferenca = abs((outro.saida_prevista - partida.saida_prevista).total_seconds())
            if diferenca > 0:
                vizinhos.append(diferenca)
        if not vizinhos:
            return None
        intervalo = min(vizinhos)

    if intervalo is None or intervalo <= 0:
        return None
    return min(TOLERANCIA_MAXIMA_SEGUNDOS, intervalo / 2)


def viagem_cumprida(partida, observada, programadas):
    """A trip must cover >=80% and depart inside the inclusive tolerance."""
    if observada is None or observada.cobertura_tracado < COBERTURA_MINIMA:
        return False
    tolerancia = _tolerancia_saida(partida, programadas)
    if tolerancia is None:
        return False
    diferenca = abs((observada.saida_observada - partida.saida_prevista).total_seconds())
    return diferenca <= tolerancia


def calcular_cumprimento(programadas, observadas):
    """Return numerator, denominator and percent; no denominator means NULL."""
    programadas = list(programadas)
    por_partida = (
        observadas if isinstance(observadas, dict)
        else {item.partida_programada_id: item for item in observadas}
    )
    cumpridas = sum(
        viagem_cumprida(partida, por_partida.get(partida.id), programadas)
        for partida in programadas
    )
    quantidade = len(programadas)
    percentual = None if quantidade == 0 else (
        (Decimal(cumpridas * 100) / Decimal(quantidade))
        .quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    )
    return ResultadoCumprimento(cumpridas, quantidade, percentual)


def _chave_publicacao(*, linha, sentido, inicio, fim, versao_gtfs_id,
                      versao_correspondencia, versao_reconstrucao, versao_calculo,
                      versoes_programacao):
    identidade = {
        "tipo": "CUMPRIMENTO_VIAGENS", "linha": linha, "sentido": sentido,
        "inicio": inicio.isoformat(), "fim": fim.isoformat(),
        "versao_gtfs_id": versao_gtfs_id,
        "versao_correspondencia": versao_correspondencia,
        "versao_reconstrucao": versao_reconstrucao,
        "versao_calculo": versao_calculo,
        "versoes_programacao": versoes_programacao,
    }
    return sha256(json.dumps(identidade, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def calcular_e_publicar_cumprimento(
    *, linha, sentido, inicio, fim, versao_gtfs_id,
    versao_correspondencia, versao_reconstrucao,
    versao_calculo=VERSAO_ALGORITMO,
):
    """Calculate for one line/direction/window and persist the versioned result.

    GTFS is already canonicalized to direction ``0``/``1``. For duplicate
    captures of a service-day timetable, prefer official data, then the most
    recently verified capture (the same policy used by GET /partidas).
    """
    if sentido not in ("0", "1"):
        raise ValueError("sentido deve ser '0' (ida) ou '1' (volta)")
    if not linha or not versao_gtfs_id or not versao_correspondencia or not versao_reconstrucao:
        raise ValueError("linha e versões GTFS, correspondência e reconstrução são obrigatórias")
    for nome, instante in (("inicio", inicio), ("fim", fim)):
        if not isinstance(instante, datetime) or instante.tzinfo is None or instante.utcoffset() is None:
            raise ValueError(f"{nome} deve conter fuso horário")
    inicio = inicio.astimezone(timezone.utc)
    fim = fim.astimezone(timezone.utc)
    if fim <= inicio:
        raise ValueError("fim deve ser posterior a inicio")

    capturas = (
        db.session.query(PartidaProgramada, ImportacaoProgramacao)
        .join(ImportacaoProgramacao, PartidaProgramada.importacao_id == ImportacaoProgramacao.id)
        .filter(
            ImportacaoProgramacao.versao_gtfs_id == versao_gtfs_id,
            ImportacaoProgramacao.linha_codigo == linha,
            ImportacaoProgramacao.sentido == sentido,
            PartidaProgramada.saida_prevista >= inicio,
            PartidaProgramada.saida_prevista < fim,
        )
        .order_by(PartidaProgramada.saida_prevista, PartidaProgramada.ordem)
        .all()
    )
    preferidas = {}
    for partida, importacao in capturas:
        contexto = (importacao.data_operacao, importacao.tipo_dia)
        atual = preferidas.get(contexto)
        prioridade = (importacao.fonte == "OFICIAL", importacao.verificada_em,
                      importacao.capturada_em, importacao.id)
        if atual is None or prioridade > atual[0]:
            preferidas[contexto] = (prioridade, importacao.id)
    ids_importacoes = {item[1] for item in preferidas.values()}
    programadas = [p for p, _ in capturas if p.importacao_id in ids_importacoes]
    fontes_programacao = sorted({
        (importacao.id, importacao.fonte, importacao.versao_fonte)
        for _, importacao in capturas if importacao.id in ids_importacoes
    })
    versoes_programacao = [
        {"importacao_id": item[0], "fonte": item[1], "versao_fonte": item[2]}
        for item in fontes_programacao
    ]

    ids_partidas = [p.id for p in programadas]
    observadas = []
    if ids_partidas:
        observadas = (
            ViagemObservada.query
            .filter(ViagemObservada.partida_programada_id.in_(ids_partidas))
            .filter(ViagemObservada.versao_reconstrucao == versao_reconstrucao)
            .filter(ViagemObservada.versao_correspondencia == versao_correspondencia)
            .all()
        )
    resultado = calcular_cumprimento(programadas, observadas)

    chave = _chave_publicacao(
        linha=linha, sentido=sentido, inicio=inicio, fim=fim,
        versao_gtfs_id=versao_gtfs_id,
        versao_correspondencia=versao_correspondencia,
        versao_reconstrucao=versao_reconstrucao, versao_calculo=versao_calculo,
        versoes_programacao=versoes_programacao,
    )
    registro = IndicadorOperacional.query.filter_by(chave_resultado=chave).one_or_none()
    if registro is None:
        registro = IndicadorOperacional(chave_resultado=chave)
        db.session.add(registro)
    registro.versao_gtfs_id = versao_gtfs_id
    registro.linha_codigo = linha
    registro.sentido = sentido
    registro.parada_codigo = None
    registro.trecho_id = None
    registro.indicador = "CUMPRIMENTO_VIAGENS"
    registro.valor = float(resultado.percentual) if resultado.percentual is not None else None
    registro.unidade = "%"
    registro.periodo_inicio = inicio
    registro.periodo_fim = fim
    registro.contagem_numerador = resultado.cumpridas
    registro.contagem_denominador = resultado.programadas
    registro.versao_conjunto = chave[:32]
    registro.versao_correspondencia = versao_correspondencia
    registro.versao_calculo = versao_calculo
    registro.qualidade = {
        "observacoes_viagem": len(observadas),
        "partidas_programadas": resultado.programadas,
        "cobertura_tracado_minima": COBERTURA_MINIMA,
        "tolerancia_saida_maxima_segundos": TOLERANCIA_MAXIMA_SEGUNDOS,
        "intervalo_horario_fixo": "menor intervalo positivo até partida vizinha da mesma grade; uma vizinha nas extremidades",
        "origem_programacao": "OFICIAL preferencial; GTFS como fallback",
        "capturas_programacao": versoes_programacao,
        "versao_reconstrucao": versao_reconstrucao,
    }
    db.session.commit()
    return registro
