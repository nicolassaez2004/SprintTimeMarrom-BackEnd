"""Reprocessamento determinístico de posições GPS sem alterar os dados brutos."""

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from uuid import uuid4

from sqlalchemy import func, or_, update
from sqlalchemy.exc import IntegrityError

from app import db
from app.models import (
    ExecucaoProcessamento,
    ObservacaoTratada,
    RegistroBrutoGPS,
    ResultadoReprocessamento,
)


class ReprocessamentoInvalido(ValueError):
    """Parâmetros insuficientes ou incompatíveis com o manifesto."""


class ReprocessamentoOcupado(RuntimeError):
    """Outra execução ainda possui a reserva deste manifesto."""


TEMPO_RESERVA = timedelta(minutes=5)


def _exigir_versao(valor, nome):
    if not isinstance(valor, str) or not valor.strip():
        raise ReprocessamentoInvalido(f"{nome} é obrigatória")
    return valor.strip()


def criar_execucao_reprocessamento(
    *, periodo_inicio, periodo_fim, versao_base, versao_codigo, versao_regra,
    versao_gtfs_id=None, chave_logica=None,
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
    versao_base = _exigir_versao(versao_base, "versao_base")
    versao_codigo = _exigir_versao(versao_codigo, "versao_codigo")
    versao_regra = _exigir_versao(versao_regra, "versao_regra")
    parametros = {
        "periodo": {
            "campo": "coletado_em",
            "inicio": inicio.isoformat(),
            "fim_exclusivo": fim.isoformat(),
        },
        "snapshot": {"bruto_id_maximo": max_id},
        "versao_gtfs_id": versao_gtfs_id,
    }
    identidade = {
        "parametros": parametros,
        "versao_base": versao_base,
        "versao_codigo": versao_codigo,
        "versao_regra": versao_regra,
    }
    chave = chave_logica or "reprocessamento:" + sha256(
        json.dumps(identidade, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    chave = _exigir_versao(chave, "chave_logica")

    existente = ExecucaoProcessamento.query.filter_by(chave_logica=chave).first()
    if existente is not None:
        if (existente.parametros != parametros
                or existente.versao_base != versao_base
                or existente.versao_codigo != versao_codigo
                or existente.versao_regra != versao_regra):
            raise ReprocessamentoInvalido(
                "chave_logica já usada com parâmetros ou versões diferentes"
            )
        return existente

    execucao = ExecucaoProcessamento(
        tipo="REPROCESSAMENTO",
        chave_logica=chave,
        parametros=parametros,
        periodo_inicio=inicio,
        periodo_fim=fim,
        bruto_id_maximo=max_id,
        versao_base=versao_base,
        versao_codigo=versao_codigo,
        versao_regra=versao_regra,
        versao_gtfs_id=versao_gtfs_id,
        estado="AGUARDANDO",
        tentativas=0,
    )
    db.session.add(execucao)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        existente = ExecucaoProcessamento.query.filter_by(chave_logica=chave).first()
        if existente is None:
            raise
        if (existente.parametros != parametros
                or existente.versao_base != versao_base
                or existente.versao_codigo != versao_codigo
                or existente.versao_regra != versao_regra):
            raise ReprocessamentoInvalido(
                "chave_logica já usada com parâmetros ou versões diferentes"
            )
        return existente
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
    if manifesto.estado == "PRONTO":
        return ResultadoReprocessamento.query.filter_by(
            execucao_processamento_id=manifesto.id
        ).count()

    agora = datetime.now(timezone.utc)
    token = str(uuid4())
    reserva_expirada = or_(
        ExecucaoProcessamento.reserva_ate.is_(None),
        ExecucaoProcessamento.reserva_ate <= agora,
    )
    claim = db.session.execute(
        update(ExecucaoProcessamento)
        .where(
            ExecucaoProcessamento.id == execucao_id,
            or_(
                ExecucaoProcessamento.estado.in_(("AGUARDANDO", "ERRO")),
                (ExecucaoProcessamento.estado == "PROCESSANDO") & reserva_expirada,
            ),
        )
        .values(
            estado="PROCESSANDO",
            tentativas=ExecucaoProcessamento.tentativas + 1,
            iniciada_em=func.coalesce(ExecucaoProcessamento.iniciada_em, agora),
            reserva_token=token,
            reserva_ate=agora + TEMPO_RESERVA,
            erro=None,
        )
    )
    if claim.rowcount != 1:
        db.session.rollback()
        manifesto = db.session.get(ExecucaoProcessamento, execucao_id)
        if manifesto and manifesto.estado == "PRONTO":
            return ResultadoReprocessamento.query.filter_by(
                execucao_processamento_id=manifesto.id
            ).count()
        raise ReprocessamentoOcupado("manifesto reservado por outro worker")
    db.session.commit()

    try:
        while True:
            agora = datetime.now(timezone.utc)
            renovacao = db.session.execute(
                update(ExecucaoProcessamento)
                .where(
                    ExecucaoProcessamento.id == execucao_id,
                    ExecucaoProcessamento.estado == "PROCESSANDO",
                    ExecucaoProcessamento.reserva_token == token,
                )
                .values(reserva_ate=agora + TEMPO_RESERVA)
            )
            if renovacao.rowcount != 1:
                raise ReprocessamentoOcupado("reserva do manifesto foi perdida")
            manifesto = db.session.get(ExecucaoProcessamento, execucao_id)
            ja_processados = (
                db.session.query(ResultadoReprocessamento.bruto_id)
                .filter(ResultadoReprocessamento.execucao_processamento_id == manifesto.id)
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
                db.session.commit()
                break

            observacoes_atuais = {
                observacao.bruto_id: observacao
                for observacao in ObservacaoTratada.query.filter(
                    ObservacaoTratada.bruto_id.in_([bruto.id for bruto in lote])
                ).all()
            }
            resultados = []
            for bruto in lote:
                resultado = processador(bruto, manifesto)
                if not isinstance(resultado, dict) or "valida" not in resultado:
                    raise ReprocessamentoInvalido(
                        "processador deve retornar um dicionário com 'valida'"
                    )
                valida = bool(resultado["valida"])
                motivo = resultado.get("motivo_invalidez")
                versao_gtfs = manifesto.versao_gtfs_id
                resultados.append(ResultadoReprocessamento(
                    execucao_processamento_id=manifesto.id,
                    bruto_id=bruto.id,
                    valida=valida,
                    motivo_invalidez=motivo,
                    versao_correspondencia=manifesto.versao_regra,
                    versao_gtfs_id=versao_gtfs,
                ))
                # ObservacaoTratada é a visão atual 1:1 do diagrama; o snapshot
                # de cada execução/versionamento fica na tabela acima.
                observacao = observacoes_atuais.get(bruto.id)
                if observacao is None:
                    observacao = ObservacaoTratada(bruto_id=bruto.id)
                    db.session.add(observacao)
                observacao.valida = valida
                observacao.motivo_invalidez = motivo
                observacao.versao_correspondencia = manifesto.versao_regra
                observacao.versao_gtfs_id = versao_gtfs
            db.session.add_all(resultados)
            # Revalida a posse depois do trabalho do lote e antes do commit.
            agora = datetime.now(timezone.utc)
            renovacao = db.session.execute(
                update(ExecucaoProcessamento)
                .where(
                    ExecucaoProcessamento.id == execucao_id,
                    ExecucaoProcessamento.estado == "PROCESSANDO",
                    ExecucaoProcessamento.reserva_token == token,
                )
                .values(reserva_ate=agora + TEMPO_RESERVA)
            )
            if renovacao.rowcount != 1:
                raise ReprocessamentoOcupado("reserva do manifesto foi perdida")
            db.session.commit()

        finalizacao = db.session.execute(
            update(ExecucaoProcessamento)
            .where(
                ExecucaoProcessamento.id == execucao_id,
                ExecucaoProcessamento.estado == "PROCESSANDO",
                ExecucaoProcessamento.reserva_token == token,
            )
            .values(
                estado="PRONTO",
                finalizada_em=datetime.now(timezone.utc),
                reserva_token=None,
                reserva_ate=None,
            )
        )
        if finalizacao.rowcount != 1:
            raise ReprocessamentoOcupado("reserva do manifesto foi perdida")
        db.session.commit()
    except Exception as erro:
        db.session.rollback()
        db.session.execute(
            update(ExecucaoProcessamento)
            .where(
                ExecucaoProcessamento.id == execucao_id,
                ExecucaoProcessamento.estado == "PROCESSANDO",
                ExecucaoProcessamento.reserva_token == token,
            )
            .values(
                estado="ERRO",
                finalizada_em=datetime.now(timezone.utc),
                erro=type(erro).__name__,
                reserva_token=None,
                reserva_ate=None,
            )
        )
        db.session.commit()
        raise

    return ResultadoReprocessamento.query.filter_by(
        execucao_processamento_id=execucao_id
    ).count()
