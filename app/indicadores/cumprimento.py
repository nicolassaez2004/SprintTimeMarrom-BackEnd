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
from app.repositories.programacao import consultar_por_dia

COBERTURA_MINIMA = 0.80
TOLERANCIA_MAXIMA_SEGUNDOS = 15 * 60
# 1.1: intervalo fixo inferido da grade completa do dia (não da janela) e
# seleção de grade pela política da US04 (captura OFICIAL vazia prevalece).
VERSAO_ALGORITMO = "cumprimento-1.1"


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
        if not any(item.id == partida.id for item in mesma_grade):
            return None
        # Partidas simultâneas (mesmo horário) não definem intervalo: usa-se a
        # partida mais próxima com horário diferente, de cada lado.
        vizinhos = [
            abs((outro.saida_prevista - partida.saida_prevista).total_seconds())
            for outro in mesma_grade if outro.saida_prevista != partida.saida_prevista
        ]
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


def calcular_cumprimento(programadas, observadas, grade=None):
    """Return numerator, denominator and percent; no denominator means NULL.

    ``grade`` is the full timetable used to infer fixed-schedule headways; the
    window must not change a trip's tolerance. Defaults to ``programadas``.
    """
    programadas = list(programadas)
    grade = programadas if grade is None else list(grade)
    por_partida = (
        observadas if isinstance(observadas, dict)
        else {item.partida_programada_id: item for item in observadas}
    )
    cumpridas = sum(
        viagem_cumprida(partida, por_partida.get(partida.id), grade)
        for partida in programadas
    )
    quantidade = len(programadas)
    percentual = None if quantidade == 0 else (
        (Decimal(cumpridas * 100) / Decimal(quantidade))
        .quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    )
    return ResultadoCumprimento(cumpridas, quantidade, percentual)


def programacao_da_janela(*, linha, sentido, inicio, fim, versao_gtfs_id):
    """Select one timetable per service day and the departures inside the window.

    Returns ``(grade, programadas, versoes_programacao)``: every departure of the
    selected captures (for headways), those with ``inicio <= saida < fim``, and
    the captures' provenance. Selection reuses the US04 policy: OFICIAL beats
    GTFS even when empty, then the latest verification. Two day types for the
    same service date are ambiguous and raise instead of being summed.
    """
    if sentido not in ("0", "1"):
        raise ValueError("sentido deve ser '0' (ida) ou '1' (volta)")
    if not linha or not versao_gtfs_id:
        raise ValueError("linha e versão GTFS são obrigatórias")
    for nome, instante in (("inicio", inicio), ("fim", fim)):
        if not isinstance(instante, datetime) or instante.utcoffset() is None:
            raise ValueError(f"{nome} deve conter fuso horário")
    inicio, fim = inicio.astimezone(timezone.utc), fim.astimezone(timezone.utc)
    if fim <= inicio:
        raise ValueError("fim deve ser posterior a inicio")

    datas = [item[0] for item in (
        db.session.query(ImportacaoProgramacao.data_operacao)
        .join(PartidaProgramada, PartidaProgramada.importacao_id == ImportacaoProgramacao.id)
        .filter(
            ImportacaoProgramacao.versao_gtfs_id == versao_gtfs_id,
            ImportacaoProgramacao.linha_codigo == linha,
            ImportacaoProgramacao.sentido == sentido,
            PartidaProgramada.saida_prevista >= inicio,
            PartidaProgramada.saida_prevista < fim,
        )
        .distinct().order_by(ImportacaoProgramacao.data_operacao).all()
    )]
    grade, versoes_programacao = [], []
    for data in datas:
        importacao, tipos = consultar_por_dia(versao_gtfs_id, linha, sentido, data)
        if importacao is None:
            raise ValueError(
                f"programação ambígua em {data.isoformat()}: tipos de dia {sorted(tipos)}"
            )
        grade.extend(importacao.partidas)
        versoes_programacao.append({
            "importacao_id": importacao.id, "fonte": importacao.fonte,
            "versao_fonte": importacao.versao_fonte,
            "data_operacao": importacao.data_operacao.isoformat(),
        })
    versoes_programacao.sort(key=lambda item: (item["importacao_id"], item["fonte"], item["versao_fonte"]))
    grade.sort(key=lambda p: (p.saida_prevista, p.ordem, p.id))
    programadas = [p for p in grade if inicio <= p.saida_prevista < fim]
    return grade, programadas, versoes_programacao


def viagens_observadas(programadas, versao_reconstrucao, versao_correspondencia):
    """Persisted trips of exactly these departures and versions."""
    ids = [p.id for p in programadas]
    if not ids:
        return []
    return (
        ViagemObservada.query
        .filter(ViagemObservada.partida_programada_id.in_(ids))
        .filter(ViagemObservada.versao_reconstrucao == versao_reconstrucao)
        .filter(ViagemObservada.versao_correspondencia == versao_correspondencia)
        .order_by(ViagemObservada.partida_programada_id, ViagemObservada.id)
        .all()
    )


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


def calcular_indicador_cumprimento(
    *, linha, sentido, inicio, fim, versao_gtfs_id,
    versao_correspondencia, versao_reconstrucao,
    versao_calculo=VERSAO_ALGORITMO,
):
    """Calculate one line/direction/window without writing to the database.

    Returns the columns of ``IndicadorOperacional`` (including ``chave_resultado``);
    ``calcular_e_publicar_cumprimento`` persists them and US12-T04 compares them.
    """
    if not versao_correspondencia or not versao_reconstrucao:
        raise ValueError("versões de correspondência e reconstrução são obrigatórias")
    grade, programadas, versoes_programacao = programacao_da_janela(
        linha=linha, sentido=sentido, inicio=inicio, fim=fim, versao_gtfs_id=versao_gtfs_id,
    )
    inicio, fim = inicio.astimezone(timezone.utc), fim.astimezone(timezone.utc)
    observadas = viagens_observadas(programadas, versao_reconstrucao, versao_correspondencia)
    resultado = calcular_cumprimento(programadas, observadas, grade)

    chave = _chave_publicacao(
        linha=linha, sentido=sentido, inicio=inicio, fim=fim,
        versao_gtfs_id=versao_gtfs_id,
        versao_correspondencia=versao_correspondencia,
        versao_reconstrucao=versao_reconstrucao, versao_calculo=versao_calculo,
        versoes_programacao=versoes_programacao,
    )
    return {
        "chave_resultado": chave,
        "versao_gtfs_id": versao_gtfs_id,
        "linha_codigo": linha,
        "sentido": sentido,
        "parada_codigo": None,
        "trecho_id": None,
        "indicador": "CUMPRIMENTO_VIAGENS",
        "valor": float(resultado.percentual) if resultado.percentual is not None else None,
        "unidade": "%",
        "periodo_inicio": inicio,
        "periodo_fim": fim,
        "contagem_numerador": resultado.cumpridas,
        "contagem_denominador": resultado.programadas,
        "versao_conjunto": chave[:32],
        "versao_correspondencia": versao_correspondencia,
        "versao_calculo": versao_calculo,
        "qualidade": {
            "observacoes_viagem": len(observadas),
            "partidas_programadas": resultado.programadas,
            "cobertura_tracado_minima": COBERTURA_MINIMA,
            "tolerancia_saida_maxima_segundos": TOLERANCIA_MAXIMA_SEGUNDOS,
            "intervalo_horario_fixo": (
                "menor intervalo positivo até a partida vizinha na grade completa do dia "
                "operacional (independe da janela); uma vizinha nas extremidades"
            ),
            "origem_programacao": "OFICIAL preferencial, inclusive captura vazia; GTFS como fallback",
            "capturas_programacao": versoes_programacao,
            "versao_reconstrucao": versao_reconstrucao,
        },
    }


def gravar_indicador(campos):
    """Upsert idempotente pela ``chave_resultado`` e commit (publicação)."""
    registro = IndicadorOperacional.query.filter_by(
        chave_resultado=campos["chave_resultado"]).one_or_none()
    if registro is None:
        registro = IndicadorOperacional(chave_resultado=campos["chave_resultado"])
        db.session.add(registro)
    for nome, valor in campos.items():
        setattr(registro, nome, valor)
    db.session.commit()
    return registro


def calcular_e_publicar_cumprimento(**parametros):
    """Calculate for one line/direction/window and persist the versioned result."""
    return gravar_indicador(calcular_indicador_cumprimento(**parametros))
