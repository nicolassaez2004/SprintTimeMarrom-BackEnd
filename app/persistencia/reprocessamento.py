"""Reprocessamento determinístico de posições GPS sem alterar os dados brutos."""

from datetime import datetime, timezone

from sqlalchemy import func

from app import db
from app.models import (
    ExecucaoProcessamento,
    ObservacaoTratada,
    RegistroBrutoGPS,
)


class ReprocessamentoInvalido(ValueError):
    """Parâmetros insuficientes ou incompatíveis com o manifesto."""


def _exigir_versao(valor, nome):
    if not isinstance(valor, str) or not valor.strip():
        raise ReprocessamentoInvalido(f"{nome} é obrigatória")
    return valor.strip()


def criar_execucao_reprocessamento(
    *, periodo_inicio, periodo_fim, versao_base, versao_codigo, versao_regra,
    versao_gtfs_id=None,
):
    """Cria e confirma o manifesto, incluindo um limite de snapshot dos brutos.

    O período usa `coletado_em` e intervalo semiaberto [início, fim). O limite
    de ID impede que registros recebidos depois alterem o conjunto da execução.
    """
    for nome, instante in (("periodo_inicio", periodo_inicio), ("periodo_fim", periodo_fim)):
        if instante is None or instante.tzinfo is None:
            raise ReprocessamentoInvalido(f"{nome} deve ter fuso horário")
    inicio = periodo_inicio.astimezone(timezone.utc)
    fim = periodo_fim.astimezone(timezone.utc)
    if fim <= inicio:
        raise ReprocessamentoInvalido("periodo_fim deve ser posterior ao início")

    max_id = db.session.query(func.max(RegistroBrutoGPS.id)).scalar() or 0
    execucao = ExecucaoProcessamento(
        periodo_inicio=inicio,
        periodo_fim=fim,
        bruto_id_maximo=max_id,
        versao_base=_exigir_versao(versao_base, "versao_base"),
        versao_codigo=_exigir_versao(versao_codigo, "versao_codigo"),
        versao_regra=_exigir_versao(versao_regra, "versao_regra"),
        versao_gtfs_id=versao_gtfs_id,
        estado="EM_ANDAMENTO",
        tentativas=0,
    )
    db.session.add(execucao)
    db.session.commit()
    return execucao


def tratar_bruto_basico(bruto, manifesto):
    """Tratamento puro inicial; correspondência de rede fica para US03."""
    ausentes = []
    if bruto.latitude is None:
        ausentes.append("latitude ausente ou inválida")
    if bruto.longitude is None:
        ausentes.append("longitude ausente ou inválida")
    if bruto.horario_gps is None:
        ausentes.append("horário GPS ausente ou inválido")
    return {
        "valida": not ausentes,
        "motivo_invalidez": "; ".join(ausentes) or None,
        "versao_correspondencia": manifesto.versao_regra,
        "versao_gtfs_id": manifesto.versao_gtfs_id,
    }


def executar_reprocessamento(execucao_id, *, processador=tratar_bruto_basico, tamanho_lote=500):
    """Processa lotes ordenados; cada lote tratado tem sua própria transação.

    Uma execução falha pode ser retomada com o mesmo ID: lotes já confirmados
    são ignorados, e o UNIQUE (execução, bruto) protege repetições concorrentes.
    """
    if not isinstance(tamanho_lote, int) or tamanho_lote < 1:
        raise ReprocessamentoInvalido("tamanho_lote deve ser um inteiro positivo")
    manifesto = db.session.get(ExecucaoProcessamento, execucao_id)
    if manifesto is None:
        raise ReprocessamentoInvalido("execução de reprocessamento inexistente")
    if manifesto.estado == "CONCLUIDA":
        return ObservacaoTratada.query.filter_by(
            execucao_processamento_id=manifesto.id
        ).count()

    manifesto.estado = "EM_ANDAMENTO"
    manifesto.tentativas += 1
    manifesto.erro = None
    db.session.commit()

    try:
        while True:
            ja_processados = (
                db.session.query(ObservacaoTratada.bruto_id)
                .filter(ObservacaoTratada.execucao_processamento_id == manifesto.id)
            )
            lote = (
                RegistroBrutoGPS.query
                .filter(RegistroBrutoGPS.id <= manifesto.bruto_id_maximo)
                .filter(RegistroBrutoGPS.coletado_em >= manifesto.periodo_inicio)
                .filter(RegistroBrutoGPS.coletado_em < manifesto.periodo_fim)
                .filter(~RegistroBrutoGPS.id.in_(ja_processados))
                .order_by(RegistroBrutoGPS.id.asc())
                .limit(tamanho_lote)
                .all()
            )
            if not lote:
                break

            resultados = []
            for bruto in lote:
                resultado = processador(bruto, manifesto)
                if not isinstance(resultado, dict) or "valida" not in resultado:
                    raise ReprocessamentoInvalido(
                        "processador deve retornar um dicionário com 'valida'"
                    )
                resultados.append(ObservacaoTratada(
                    execucao_processamento_id=manifesto.id,
                    bruto_id=bruto.id,
                    valida=bool(resultado["valida"]),
                    motivo_invalidez=resultado.get("motivo_invalidez"),
                    versao_correspondencia=manifesto.versao_regra,
                    versao_gtfs_id=manifesto.versao_gtfs_id,
                ))
            db.session.add_all(resultados)
            db.session.commit()

        manifesto = db.session.get(ExecucaoProcessamento, execucao_id)
        manifesto.estado = "CONCLUIDA"
        manifesto.finalizada_em = datetime.now(timezone.utc)
        db.session.commit()
    except Exception as erro:
        db.session.rollback()
        manifesto = db.session.get(ExecucaoProcessamento, execucao_id)
        manifesto.estado = "FALHA"
        manifesto.finalizada_em = datetime.now(timezone.utc)
        manifesto.erro = type(erro).__name__
        db.session.commit()
        raise

    return ObservacaoTratada.query.filter_by(
        execucao_processamento_id=execucao_id
    ).count()
