"""Validação HTTP; resposta em docs/schemas/partidas-resposta.schema.json."""

import re
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from app.programacao.normalizacao import SENTIDOS, TIPOS_DIA


class FiltroInvalido(ValueError):
    def __init__(self, campo, mensagem):
        self.campo = campo
        super().__init__(mensagem)


@dataclass(frozen=True)
class FiltrosPartidas:
    linha: str
    sentido: str
    data: date
    tipo_dia: str | None = None
    versao_gtfs_id: str | None = None


def ler_filtros(parametros):
    permitidos = {"linha", "sentido", "data", "tipo_dia", "versao_gtfs_id"}
    for campo in parametros:
        if campo not in permitidos:
            raise FiltroInvalido(campo, "Filtro não reconhecido.")
        if len(parametros.getlist(campo)) != 1:
            raise FiltroInvalido(campo, "Informe cada filtro uma única vez.")
    for campo in ("linha", "sentido", "data"):
        if not parametros.get(campo, "").strip():
            raise FiltroInvalido(campo, "Filtro obrigatório.")
    # route_id GTFS é textual e sensível a maiúsculas e espaços no modelo.
    linha = parametros["linha"]
    if len(linha) > 64 or any(ord(caractere) < 32 or ord(caractere) == 127 for caractere in linha):
        raise FiltroInvalido("linha", "Informe um código de linha com até 64 caracteres, sem controles.")
    sentido = SENTIDOS.get(parametros["sentido"].strip().lower())
    if sentido is None:
        raise FiltroInvalido("sentido", "Informe ida/0 ou volta/1, conforme GTFS.")
    valor_data = parametros["data"].strip()
    try:
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", valor_data):
            raise ValueError()
        data_operacao = date.fromisoformat(valor_data)
    except ValueError as erro:
        raise FiltroInvalido("data", "Informe uma data válida no formato YYYY-MM-DD.") from erro
    tipo_dia = None
    if "tipo_dia" in parametros:
        tipo_dia = parametros["tipo_dia"].strip().upper()
        if tipo_dia not in TIPOS_DIA:
            raise FiltroInvalido("tipo_dia", "Informe UTIL, SABADO, DOMINGO ou FERIADO.")
    versao_id = None
    if "versao_gtfs_id" in parametros:
        valor = parametros["versao_gtfs_id"].strip().lower()
        try:
            versao_id = str(UUID(valor))
            if valor != versao_id:
                raise ValueError()
        except ValueError as erro:
            raise FiltroInvalido("versao_gtfs_id", "Informe um UUID no formato com hífens.") from erro
    return FiltrosPartidas(linha, sentido, data_operacao, tipo_dia, versao_id)
