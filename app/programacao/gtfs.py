"""Geração de partidas da rede GTFS para uma data operacional (US04-T02A)."""

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.gtfs.service import GTFSValidationError, _abrir_versao, _ler_csv, _unicos

_DIAS_GTFS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


@dataclass(frozen=True)
class OfertaGTFS:
    saida_prevista: datetime
    tipo_oferta: str
    intervalo_seg: int | None = None


def _fuso_da_rota(arquivo, rota):
    agencias = list(_ler_csv(arquivo, "agency.txt"))
    if not agencias:
        raise GTFSValidationError("agency.txt não contém agência")
    por_id = {}
    for numero, agencia in agencias:
        agencia_id = agencia.get("agency_id") or None
        if len(agencias) > 1 and not agencia_id:
            raise GTFSValidationError(f"agency.txt linha {numero}: agency_id obrigatório")
        if agencia_id in por_id:
            raise GTFSValidationError(f"agency.txt: agency_id {agencia_id!r} repetido")
        por_id[agencia_id] = agencia

    rota_agencia = rota.get("agency_id") or None
    if rota_agencia is None and len(agencias) == 1:
        agencia = agencias[0][1]
    else:
        agencia = por_id.get(rota_agencia)
    if agencia is None:
        raise GTFSValidationError("routes.txt: agency_id não resolve uma agência")
    try:
        return ZoneInfo(agencia.get("agency_timezone", ""))
    except (ValueError, TypeError, ZoneInfoNotFoundError):
        raise GTFSValidationError("agency.txt: agency_timezone inválido") from None


def _data_gtfs(valor, arquivo, numero, campo):
    try:
        if not re.fullmatch(r"\d{8}", valor or ""):
            raise ValueError
        return datetime.strptime(valor, "%Y%m%d").date()
    except ValueError:
        raise GTFSValidationError(f"{arquivo} linha {numero}: {campo} inválida") from None


def _segundos_gtfs(valor, numero, arquivo="stop_times.txt"):
    if not isinstance(valor, str) or not re.fullmatch(r"\d{2}:[0-5]\d:[0-5]\d", valor):
        raise GTFSValidationError(f"{arquivo} linha {numero}: horário inválido")
    hora, minuto, segundo = (int(parte) for parte in valor.split(":"))
    if hora > 99:
        raise GTFSValidationError(f"{arquivo} linha {numero}: horário inválido")
    return hora * 3600 + minuto * 60 + segundo


def servicos_ativos(arquivo, data_operacao):
    """Resolve calendar.txt e calendar_dates.txt, com exceções sobrescrevendo a regra."""
    if isinstance(data_operacao, datetime) or not isinstance(data_operacao, date):
        raise TypeError("data_operacao deve ser date, sem inferir a data de um instante")

    calendario = _unicos(_ler_csv(arquivo, "calendar.txt", obrigatorio=False) or (),
                         "calendar.txt", "service_id")
    excecoes = {}
    for numero, linha in _ler_csv(arquivo, "calendar_dates.txt", obrigatorio=False) or ():
        servico = linha.get("service_id")
        if not servico:
            raise GTFSValidationError(f"calendar_dates.txt linha {numero}: service_id vazio")
        dia = _data_gtfs(linha.get("date", ""), "calendar_dates.txt", numero, "date")
        try:
            tipo = int(linha.get("exception_type", ""))
        except ValueError:
            tipo = 0
        if tipo not in (1, 2):
            raise GTFSValidationError(
                f"calendar_dates.txt linha {numero}: exception_type deve ser 1 ou 2"
            )
        chave = (servico, dia)
        if chave in excecoes:
            raise GTFSValidationError(
                f"calendar_dates.txt linha {numero}: exceção de serviço/data repetida"
            )
        excecoes[chave] = tipo

    if not calendario and not excecoes:
        raise GTFSValidationError("feed não contém calendar.txt nem calendar_dates.txt")

    ativos = set()
    dia_semana = _DIAS_GTFS[data_operacao.weekday()]
    for servico, (numero, linha) in calendario.items():
        try:
            valor_dia = linha.get(dia_semana)
            ativo_no_dia = valor_dia == "1"
            if valor_dia not in ("0", "1"):
                raise ValueError
            inicio = _data_gtfs(linha.get("start_date", ""), "calendar.txt", numero, "start_date")
            fim = _data_gtfs(linha.get("end_date", ""), "calendar.txt", numero, "end_date")
        except (ValueError, TypeError):
            raise GTFSValidationError(f"calendar.txt linha {numero}: calendário inválido") from None
        if fim < inicio:
            raise GTFSValidationError(f"calendar.txt linha {numero}: end_date anterior a start_date")
        if inicio <= data_operacao <= fim and ativo_no_dia:
            ativos.add(servico)

    for (servico, dia), tipo in excecoes.items():
        if dia == data_operacao:
            if tipo == 1:
                ativos.add(servico)
            else:
                ativos.discard(servico)
    return frozenset(ativos)


def gerar_partidas(versao_id, linha, sentido, data_operacao, *, diretorio=None):
    """Gera saídas de primeira parada para trips ativos na data e direção pedidas."""
    if sentido not in ("0", "1"):
        raise ValueError("sentido GTFS deve ser 0 ou 1")
    versao, arquivo = _abrir_versao(versao_id, diretorio)
    with arquivo:
        ativos = servicos_ativos(arquivo, data_operacao)
        rotas = _unicos(_ler_csv(arquivo, "routes.txt"), "routes.txt", "route_id")
        if linha not in rotas:
            raise LookupError(f"linha GTFS inexistente na versão: {linha}")
        fuso_operacional = _fuso_da_rota(arquivo, rotas[linha][1])

        viagens = _unicos(_ler_csv(arquivo, "trips.txt"), "trips.txt", "trip_id")
        selecionadas = {}
        for trip_id, (numero, trip) in viagens.items():
            route_id = trip.get("route_id")
            service_id = trip.get("service_id")
            direction_id = trip.get("direction_id")
            if not route_id or not service_id:
                raise GTFSValidationError(f"trips.txt linha {numero}: route_id/service_id vazio")
            if route_id not in rotas:
                raise GTFSValidationError(f"trips.txt linha {numero}: route_id inexistente")
            if direction_id not in ("0", "1"):
                raise GTFSValidationError(f"trips.txt linha {numero}: direction_id deve ser 0 ou 1")
            if route_id == linha and service_id in ativos and direction_id == sentido:
                selecionadas[trip_id] = None

        if not selecionadas:
            return versao, ()

        frequencias = {}
        for numero, frequencia in _ler_csv(
            arquivo, "frequencies.txt", obrigatorio=False,
        ) or ():
            trip_id = frequencia.get("trip_id")
            if trip_id not in selecionadas:
                continue
            try:
                inicio_faixa = _segundos_gtfs(
                    frequencia.get("start_time", ""), numero, "frequencies.txt",
                )
                fim_faixa = _segundos_gtfs(
                    frequencia.get("end_time", ""), numero, "frequencies.txt",
                )
                intervalo = int(frequencia.get("headway_secs", ""))
                if fim_faixa <= inicio_faixa or intervalo <= 0:
                    raise ValueError
                if frequencia.get("exact_times", "") not in ("", "0", "1"):
                    raise ValueError
            except (ValueError, TypeError):
                raise GTFSValidationError(
                    f"frequencies.txt linha {numero}: faixa ou headway inválido"
                ) from None
            frequencias.setdefault(trip_id, []).append(
                (inicio_faixa, fim_faixa, intervalo),
            )

        sequencias = {}
        for numero, stop_time in _ler_csv(arquivo, "stop_times.txt"):
            trip_id = stop_time.get("trip_id")
            if trip_id not in selecionadas:
                continue
            try:
                ordem = int(stop_time.get("stop_sequence", ""))
                if ordem < 1:
                    raise ValueError
            except ValueError:
                raise GTFSValidationError(
                    f"stop_times.txt linha {numero}: stop_sequence inválido"
                ) from None
            if ordem in sequencias.setdefault(trip_id, {}):
                raise GTFSValidationError(
                    f"stop_times.txt linha {numero}: stop_sequence repetido na viagem"
                )
            sequencias[trip_id][ordem] = (numero, stop_time)

        inicio = datetime.combine(data_operacao, time(), fuso_operacional)
        saidas = []
        for trip_id in sorted(selecionadas):
            paradas = sequencias.get(trip_id)
            if not paradas:
                raise GTFSValidationError(f"viagem {trip_id!r} ativa não tem stop_times")
            numero, primeira = paradas[min(paradas)]
            horario = primeira.get("departure_time") or primeira.get("arrival_time")
            if not horario:
                raise GTFSValidationError(
                    f"stop_times.txt linha {numero}: primeira parada sem horário de saída"
                )
            try:
                instante = inicio + timedelta(seconds=_segundos_gtfs(horario, numero))
            except OverflowError:
                raise GTFSValidationError(
                    f"stop_times.txt linha {numero}: horário excede o intervalo suportado"
                ) from None
            faixas = frequencias.get(trip_id)
            if not faixas:
                saidas.append(instante.astimezone(timezone.utc))
                continue
            for inicio_faixa, fim_faixa, intervalo in faixas:
                horario = inicio + timedelta(seconds=inicio_faixa)
                while inicio_faixa < fim_faixa:
                    saidas.append(OfertaGTFS(
                        horario.astimezone(timezone.utc), "FREQUENCIA", intervalo,
                    ))
                    inicio_faixa += intervalo
                    horario += timedelta(seconds=intervalo)
    return versao, tuple(sorted(
        saidas,
        key=lambda partida: getattr(partida, "saida_prevista", partida),
    ))
