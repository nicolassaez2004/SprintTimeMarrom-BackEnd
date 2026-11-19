"""Normalização do formato público de partidas utilizado no portal SPTrans."""

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app.integrations.programacao import (
    FonteOficialIndisponivel, ProgramacaoOficialInvalida, normalizar_linha,
)

TIPOS_DIA = {"UTIL": 0, "SABADO": 1, "DOMINGO": 2, "FERIADO": 3}
SENTIDOS = {"ida": "0", "volta": "1", "0": "0", "1": "1"}


def validar_contexto(linha, sentido, data_operacao, tipo_dia):
    codigo = normalizar_linha(linha)
    normalizado = SENTIDOS.get(str(sentido).strip().lower())
    if normalizado is None:
        raise ValueError("sentido deve ser ida/0 ou volta/1, conforme GTFS")
    if isinstance(data_operacao, datetime) or not isinstance(data_operacao, date):
        raise TypeError("data_operacao deve ser date, sem inferir a data a partir de um instante")
    tipo = str(tipo_dia).strip().upper()
    if tipo not in TIPOS_DIA:
        raise ValueError("tipo_dia deve ser UTIL, SABADO, DOMINGO ou FERIADO")
    return codigo, normalizado, data_operacao, tipo


def _segundos(valor):
    if not isinstance(valor, str) or not re.fullmatch(r"\d{2}:\d{2}(?::\d{2})?", valor):
        raise ProgramacaoOficialInvalida("horário deve usar HH:MM ou HH:MM:SS")
    partes = [int(parte) for parte in valor.split(":")]
    hora, minuto = partes[:2]
    segundo = partes[2] if len(partes) == 3 else 0
    if minuto > 59 or segundo > 59:
        raise ProgramacaoOficialInvalida("minuto ou segundo fora da faixa válida")
    return hora * 3600 + minuto * 60 + segundo


@dataclass(frozen=True)
class ProgramacaoNormalizada:
    linha: str
    sentido: str
    data_operacao: date
    tipo_dia: str
    saidas: tuple[datetime, ...]


def normalizar(captura, linha, sentido, data_operacao, tipo_dia):
    codigo, sentido, data_operacao, tipo_dia = validar_contexto(linha, sentido, data_operacao, tipo_dia)
    try:
        payload = json.loads(captura.conteudo.decode("utf-8-sig"))
    except (ValueError, UnicodeError) as erro:
        raise ProgramacaoOficialInvalida("a fonte não contém JSON válido") from erro
    if not isinstance(payload, dict) or str(payload.get("codigo", "")).strip().upper() != codigo:
        raise ProgramacaoOficialInvalida("linha da resposta não corresponde à linha solicitada")
    if payload.get("codigoErro") not in (None, 0):
        raise ProgramacaoOficialInvalida("a fonte informou erro na consulta")
    # Exigir os dois campos evita interpretar uma resposta incompleta como zero partidas.
    if not all(isinstance(payload.get(chave), list) for chave in ("partidasIda", "partidasVolta")):
        raise ProgramacaoOficialInvalida("campos de partidas ausentes ou inválidos")
    grupos = payload["partidasIda" if sentido == "0" else "partidasVolta"]
    horarios = []
    publicado = False
    for grupo in grupos:
        if not isinstance(grupo, dict) or type(grupo.get("tipoDia")) is not int:
            raise ProgramacaoOficialInvalida("grupo sem tipoDia inteiro")
        if grupo["tipoDia"] != TIPOS_DIA[tipo_dia]:
            continue
        publicado = True
        if not isinstance(grupo.get("horariosProgramados"), list):
            raise ProgramacaoOficialInvalida("horariosProgramados ausente ou inválido")
        for partida in grupo["horariosProgramados"]:
            if not isinstance(partida, dict):
                raise ProgramacaoOficialInvalida("partida deve ser um objeto")
            horarios.append(_segundos(partida.get("horario")))

    # O portal aplica ajustes específicos para feriados em JavaScript. Não
    # inferir que a ausência de um grupo no JSON significa ausência de serviço.
    if tipo_dia == "FERIADO" and not publicado:
        raise FonteOficialIndisponivel("FERIADO_NAO_PUBLICADO")

    inicio = datetime.combine(data_operacao, time(), ZoneInfo("America/Sao_Paulo"))
    saidas = []
    dia_extra = 0
    anterior = None
    for horario in horarios:
        # A fonte lista horários da madrugada depois dos noturnos. Só aceitar
        # a virada inequívoca; uma outra desordem exige revisão da entrada.
        if anterior is not None and horario < anterior:
            if 18 * 3600 <= anterior < 86400 and horario < 6 * 3600 and dia_extra == 0:
                dia_extra = 86400
            else:
                raise ProgramacaoOficialInvalida("ordem dos horários ambígua para a data operacional")
        if horario >= 86400 and dia_extra:
            raise ProgramacaoOficialInvalida("horário após 24h misturado com virada implícita")
        try:
            saidas.append((inicio + timedelta(seconds=horario + dia_extra)).astimezone(timezone.utc))
        except OverflowError as erro:
            raise ProgramacaoOficialInvalida("horário excede o intervalo de datas suportado") from erro
        anterior = horario
    return ProgramacaoNormalizada(codigo, sentido, data_operacao, tipo_dia, tuple(saidas))
