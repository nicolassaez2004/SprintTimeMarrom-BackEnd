"""Separação das observações tratadas em viagens contínuas por veículo (US16-T01)."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable


@dataclass(frozen=True)
class ObservacaoViagem:
    prefixo: str
    linha_codigo: str
    sentido_gtfs: str
    horario_gps: datetime
    valida: bool


@dataclass(frozen=True)
class ViagemSeparada:
    """Trecho contínuo de GPS de um veículo; ainda não é a entidade persistida
    `app.models.ViagemObservada`, que exige partida programada associada."""

    prefixo: str
    linha_codigo: str
    sentido_gtfs: str
    inicio: datetime
    fim: datetime
    motivo_encerramento: str


@dataclass(frozen=True)
class ResultadoSeparacao:
    viagens: tuple
    ambiguidades: tuple


class SeparadorViagens:
    """Separa observações tratadas válidas por veículo linha sentido continuidade."""

    def __init__(self, intervalo_lacuna: timedelta = timedelta(minutes=15)):
        if intervalo_lacuna <= timedelta(0):
            raise ValueError("intervalo_lacuna deve ser positivo")
        self.intervalo_lacuna = intervalo_lacuna

    def separar(self, observacoes: Iterable[ObservacaoViagem]) -> ResultadoSeparacao:
        atuais = {}
        viagens = []
        ambiguidades = []

        ordenadas = sorted(
            (o for o in observacoes if o.valida),
            key=lambda o: (o.prefixo, o.horario_gps),
        )

        for observacao in ordenadas:
            prefixo = observacao.prefixo
            atual = atuais.get(prefixo)

            if atual is not None:
                inicio, ultimo = atual
                nova_linha = ultimo.linha_codigo != observacao.linha_codigo
                novo_sentido = ultimo.sentido_gtfs != observacao.sentido_gtfs
                lacuna = observacao.horario_gps - ultimo.horario_gps

                if lacuna > self.intervalo_lacuna:
                    viagens.append(self._encerrar(inicio, ultimo.horario_gps, "lacuna"))
                    atual = None
                    ambiguidades.append(
                        f"Prefixo {prefixo}: lacuna de {lacuna.total_seconds()/60:.1f} min "
                        "entre observações"
                    )
                elif nova_linha:
                    viagens.append(self._encerrar(inicio, ultimo.horario_gps, "mudanca_linha"))
                    atual = None
                elif novo_sentido:
                    viagens.append(self._encerrar(inicio, ultimo.horario_gps, "mudanca_sentido"))
                    atual = None

            if atual is None:
                atuais[prefixo] = (observacao, observacao)
            else:
                inicio, _ = atual
                atuais[prefixo] = (inicio, observacao)

        for inicio, ultimo in atuais.values():
            viagens.append(self._encerrar(inicio, ultimo.horario_gps, "fim_da_amostra"))

        viagens.sort(key=lambda v: (v.inicio, v.fim, v.prefixo))
        return ResultadoSeparacao(tuple(viagens), tuple(ambiguidades))

    @staticmethod
    def _encerrar(
        observacao: ObservacaoViagem,
        fim: datetime,
        motivo: str,
    ) -> ViagemSeparada:
        return ViagemSeparada(
            prefixo=observacao.prefixo,
            linha_codigo=observacao.linha_codigo,
            sentido_gtfs=observacao.sentido_gtfs,
            inicio=observacao.horario_gps,
            fim=fim,
            motivo_encerramento=motivo,
        )
