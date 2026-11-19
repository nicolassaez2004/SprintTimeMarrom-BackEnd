"""Verificação diária de GTFS, executada pelo processo separado de tarefas."""

import hashlib
import json
import logging
import time
from datetime import datetime, timedelta, timezone

from app import db
from app.gtfs.service import capturar_gtfs_com_resultado
from app.models import VerificacaoGTFS

logger = logging.getLogger(__name__)
INTERVALO_DIARIO = timedelta(days=1)
INTERVALO_REPETICAO = timedelta(minutes=15)
TOLERANCIA_ATRASO = timedelta(minutes=1)


def agora_utc():
    return datetime.now(timezone.utc)


def _instante(relogio):
    instante = relogio()
    if instante.tzinfo is None:
        raise ValueError("o relógio das tarefas deve fornecer instantes com fuso")
    return instante.astimezone(timezone.utc)


def _registrar(evento, nivel=logging.INFO, **campos):
    # Não registrar exceções completas, URLs da fonte ou credenciais.
    logger.log(nivel, json.dumps({"evento": evento, **campos}, ensure_ascii=False,
                                default=lambda value: value.isoformat()))


def nome_lock_gtfs(engine):
    # GET_LOCK é por servidor: separar GTFS de GPS e desenvolvimento de teste.
    banco = engine.url.database or ""
    return "sptrans_gtfs_" + hashlib.sha256(banco.encode()).hexdigest()[:32]


def proxima_verificacao(agora):
    ultima = VerificacaoGTFS.query.order_by(
        VerificacaoGTFS.iniciada_em.desc(), VerificacaoGTFS.id.desc(),
    ).first()
    if ultima is None:
        return agora
    prevista = ultima.prevista_em or ultima.iniciada_em
    if ultima.estado == "EM_ANDAMENTO" or ultima.erro == "ProcessoInterrompido":
        return prevista
    termino = ultima.finalizada_em or ultima.iniciada_em
    if ultima.estado == "FALHA":
        return termino + INTERVALO_REPETICAO
    # Preserva a cadência após reiniciar e pula horários perdidos sem rajada.
    dias = max(1, (termino - prevista) // INTERVALO_DIARIO + 1)
    return prevista + dias * INTERVALO_DIARIO


def recuperar_interrompidas(agora):
    """Chamar somente com o lock exclusivo adquirido pelo processo de tarefas."""
    interrompidas = VerificacaoGTFS.query.filter_by(
        estado="EM_ANDAMENTO",
    ).all()
    for execucao in interrompidas:
        execucao.estado = "FALHA"
        execucao.resultado = "FALHA"
        execucao.finalizada_em = agora
        execucao.erro = "ProcessoInterrompido"
    db.session.commit()
    for execucao in interrompidas:
        _registrar("gtfs_execucao_interrompida", logging.ERROR,
                   execucao_id=execucao.id, identificada_em=execucao.identificada_em,
                   estado="FALHA")


def verificar_gtfs(prevista_em=None, *, relogio=None, cliente=None):
    relogio = relogio or agora_utc
    inicio = _instante(relogio)
    prevista_em = prevista_em or inicio
    if prevista_em.tzinfo is None:
        raise ValueError("o agendamento GTFS deve conter fuso horário")
    execucao = VerificacaoGTFS(
        prevista_em=prevista_em, iniciada_em=inicio, estado="EM_ANDAMENTO",
    )
    db.session.add(execucao)
    db.session.commit()
    execucao_id = execucao.id
    atraso = max(0, (inicio - prevista_em).total_seconds())
    if atraso >= TOLERANCIA_ATRASO.total_seconds():
        _registrar("gtfs_verificacao_atrasada", logging.WARNING,
                   execucao_id=execucao_id, prevista_em=prevista_em,
                   iniciada_em=inicio, atraso_segundos=atraso)

    resultado = {"execucao_id": execucao_id, "prevista_em": prevista_em,
                 "iniciada_em": inicio, "identificada_em": None,
                 "persistida_em": None, "tempo_ate_base_segundos": None,
                 "atraso_agendamento_segundos": atraso}

    def identificar(digest, instante):
        anterior = VerificacaoGTFS.query.filter(
            VerificacaoGTFS.hash_identificado == digest,
            VerificacaoGTFS.identificada_em.is_not(None),
        ).order_by(VerificacaoGTFS.identificada_em).first()
        primeira_identificacao = anterior.identificada_em if anterior else instante
        execucao.hash_identificado = digest
        execucao.identificada_em = primeira_identificacao
        db.session.commit()
        resultado.update(hash=digest, identificada_em=primeira_identificacao)
        return primeira_identificacao

    try:
        captura = capturar_gtfs_com_resultado(cliente=cliente, relogio=relogio,
                                              ao_identificar=identificar)
        tempo = captura.tempo_ate_base_segundos
        resultado.update(
            resultado="NOVA" if captura.nova else "REPETIDA",
            versao_id=captura.versao.id, hash=captura.versao.hash,
            identificada_em=captura.identificada_em,
            persistida_em=captura.persistida_em,
            tempo_ate_base_segundos=tempo,
            prazo_24h_atendido=(0 <= tempo <= 86400) if tempo is not None else None,
        )
        execucao.versao_id = captura.versao.id
        execucao.persistida_em = captura.persistida_em
        execucao.resultado = resultado["resultado"]
        execucao.estado = "OK"
        if tempo is not None and not resultado["prazo_24h_atendido"]:
            execucao.estado = "PARCIAL"
            execucao.erro = "PrazoGTFSExcedido" if tempo > 86400 else "RelogioRetrocedeu"
            _registrar("gtfs_prazo_excedido", logging.ERROR, **resultado)
        execucao.finalizada_em = _instante(relogio)
        db.session.commit()
    except Exception as erro:
        db.session.rollback()
        execucao = db.session.get(VerificacaoGTFS, execucao_id)
        execucao.estado = "FALHA"
        execucao.resultado = "FALHA"
        execucao.finalizada_em = _instante(relogio)
        execucao.erro = type(erro).__name__
        db.session.commit()
        resultado.update(resultado="FALHA", tipo_erro=type(erro).__name__)
    resultado["estado"] = execucao.estado
    resultado["finalizada_em"] = execucao.finalizada_em
    _registrar("gtfs_verificacao", logging.ERROR if execucao.estado == "FALHA"
               else logging.INFO, **resultado)
    return resultado


def _executar_periodicas(periodicas, agora, bloqueadas):
    """Tarefas idempotentes chamadas a cada volta (no máximo 60 s entre elas).

    A falha de uma não afeta o GTFS nem as demais; ela só volta a ser tentada
    após INTERVALO_REPETICAO, para não repetir uma rotina pesada a cada minuto.
    """
    for tarefa in periodicas:
        nome = getattr(tarefa, "__name__", repr(tarefa))
        if bloqueadas.get(nome, agora) > agora:
            continue
        try:
            tarefa(agora)
            bloqueadas.pop(nome, None)
        except Exception as erro:
            db.session.rollback()
            bloqueadas[nome] = agora + INTERVALO_REPETICAO
            _registrar("tarefa_periodica_falha", logging.ERROR,
                       tarefa=nome, tipo_erro=type(erro).__name__)
        finally:
            db.session.remove()


def executar_tarefas(*, relogio=None, dormir=None, manter_ativo=None,
                     verificar=None, max_ciclos=None, periodicas=()):
    """Ciclo imediato quando devido; depois aguarda a agenda persistida.

    Relógio e espera injetáveis permitem simular dias sem acessar rede/MySQL.
    O chamador deve manter o lock exclusivo durante toda a execução.
    `periodicas` recebe callables `tarefa(agora)` executados no mesmo processo.
    """
    relogio = relogio or agora_utc
    dormir = dormir or time.sleep
    verificar = verificar or verificar_gtfs
    manter_ativo = manter_ativo or (lambda: True)
    ciclos = 0
    recuperado = False
    bloqueadas = {}
    while max_ciclos is None or ciclos < max_ciclos:
        if not manter_ativo():
            _registrar("gtfs_lock_perdido", logging.ERROR)
            break
        try:
            _executar_periodicas(periodicas, _instante(relogio), bloqueadas)
            agora = _instante(relogio)
            if not recuperado:
                recuperar_interrompidas(agora)
                recuperado = True
            prevista = proxima_verificacao(agora)
            db.session.remove()
            espera = (prevista - agora).total_seconds()
            if espera > 0:
                # Renova a sessão do lock também durante a espera de 24 h.
                dormir(min(espera, 60))
                continue
            verificar(prevista, relogio=relogio)
            ciclos += 1
        except Exception as erro:
            db.session.rollback()
            recuperado = False
            _registrar("gtfs_tarefas_falha", logging.ERROR, tipo_erro=type(erro).__name__)
            dormir(INTERVALO_REPETICAO.total_seconds())
        finally:
            db.session.remove()
    return ciclos
