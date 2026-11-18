"""Validação de registros e mensuração da cobertura dos ciclos de coleta.

A chave lógica de uma posição é (prefixo_origem, horario_gps), conforme o
modelo de coleta. Registros sem qualquer parte dessa chave permanecem no
histórico sem deduplicação entre ciclos; valores ausentes continuam NULL.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from app import db
from app.config import validar_intervalo_coleta
from app.models import ExecucaoColeta, RegistroBrutoGPS


META_COBERTURA_MENSAL = Decimal("99")


def chave_gps_completa(registro):
    """Retorna a chave lógica quando completa; None se algum campo for NULL."""
    if registro.prefixo_origem is None or registro.horario_gps is None:
        return None
    return (registro.prefixo_origem, registro.horario_gps)


def validar_registro_gps(registro):
    """Confere os campos tipados sem preencher nem descartar campos ausentes.

    O payload bruto não é alterado. Coordenadas e horário inválidos devem ser
    convertidos para None pelo adaptador da fonte antes desta validação.
    """
    if registro.latitude is not None and not -90 <= registro.latitude <= 90:
        raise ValueError("latitude fora da faixa válida")
    if registro.longitude is not None and not -180 <= registro.longitude <= 180:
        raise ValueError("longitude fora da faixa válida")
    if registro.horario_gps is not None and registro.horario_gps.tzinfo is None:
        raise ValueError("horario_gps deve conter fuso horário")
    return registro


@dataclass(frozen=True)
class RelatorioCobertura:
    inicio: datetime
    fim: datetime
    intervalo_segundos: int
    ciclos_esperados: int
    ciclos_observados: int
    percentual: Decimal
    meta_percentual: Decimal

    @property
    def meta_atingida(self):
        return self.percentual >= self.meta_percentual


def medir_cobertura(inicio, fim, intervalo_segundos, *, meta_percentual=META_COBERTURA_MENSAL):
    """Mede a cobertura de uma janela observada; não estima período ausente.

    `inicio` e `fim` são obrigatórios e timezone-aware. A quantidade esperada
    considera os instantes de ciclo dentro da janela semiaberta [início, fim).
    """
    if inicio is None or fim is None:
        raise ValueError("início e fim do período observado são obrigatórios")
    if inicio.tzinfo is None or fim.tzinfo is None:
        raise ValueError("início e fim devem conter fuso horário")
    inicio = inicio.astimezone(timezone.utc)
    fim = fim.astimezone(timezone.utc)
    if fim <= inicio:
        raise ValueError("fim deve ser posterior ao início")
    intervalo = validar_intervalo_coleta(intervalo_segundos)
    duracao = Decimal(str((fim - inicio).total_seconds()))
    esperado = int(duracao // intervalo)
    if esperado < 1:
        raise ValueError("o período observado deve comportar ao menos um ciclo")

    observado = db.session.query(ExecucaoColeta.id).filter(
        ExecucaoColeta.iniciada_em >= inicio,
        ExecucaoColeta.iniciada_em < fim,
    ).count()
    percentual = (Decimal(observado) * Decimal(100) / Decimal(esperado)).quantize(
        Decimal("0.01")
    )
    return RelatorioCobertura(
        inicio=inicio,
        fim=fim,
        intervalo_segundos=intervalo,
        ciclos_esperados=esperado,
        ciclos_observados=observado,
        percentual=percentual,
        meta_percentual=Decimal(str(meta_percentual)),
    )


def validar_unicidade_chave(registros, *, sessao=None):
    """Filtra duplicatas do lote e posições cuja chave completa já foi gravada.

    Linhas com chave incompleta são sempre preservadas, incluindo repetições,
    pois NULL representa dado ausente e não permite inferir identidade.
    """
    sessao = sessao or db.session
    chaves = {chave_gps_completa(r) for r in registros}
    chaves.discard(None)
    existentes = set()
    if chaves:
        prefixos = {chave[0] for chave in chaves}
        horarios = {chave[1] for chave in chaves}
        linhas = sessao.query(
            RegistroBrutoGPS.prefixo_origem, RegistroBrutoGPS.horario_gps
        ).filter(
            RegistroBrutoGPS.prefixo_origem.in_(prefixos),
            RegistroBrutoGPS.horario_gps.in_(horarios),
        ).all()
        existentes = set(linhas)

    novos = []
    for registro in registros:
        chave = chave_gps_completa(registro)
        if chave is not None:
            if chave in existentes:
                continue
            existentes.add(chave)
        novos.append(registro)
    return novos
