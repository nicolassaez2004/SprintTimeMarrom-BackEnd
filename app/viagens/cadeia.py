"""Encadeamento automático do fluxo 1 do diagrama de componentes.

"BusCollector grava RegistroBrutoGPS ... Depois, jobs encadeados executam MatchingService →
TripReconstructionService → IndicatorService + QualityService." Até aqui, correspondência
(US03), partidas do dia (US04) e reconstrução (US14-T01C) só rodavam por comando manual, e a
agenda de indicadores (US23-T03) não encontrava nenhuma viagem para calcular.

``processar_cadeia(agora)`` é uma tarefa periódica de ``python -m app.tarefas``, chamada antes de
``atualizar_indicadores``. Cada etapa é independente: a falha de uma é registrada e não impede as
outras. Todas são idempotentes e limitadas por execução.
"""
from datetime import timedelta
import json
import logging

from flask import current_app
from sqlalchemy import or_

from app import db
from app.core.tempo import FUSO
from app.models import ImportacaoProgramacao, ObservacaoTratada, RegistroBrutoGPS

logger = logging.getLogger(__name__)

LOTE_CORRESPONDENCIA = 1000
MAXIMO_CORRESPONDENCIA = 50000      # por execução; o atraso acumulado é recuperado nas seguintes
JANELA_RECONSTRUCAO = timedelta(hours=3)
_geografia = {}


def garantir_programacao(dia):
    """Partidas GTFS de todas as linhas no dia (US04), uma vez por versão vigente."""
    from app.gtfs.service import VersaoGTFSAmbigua, VersaoGTFSInexistente, versao_vigente
    from app.programacao.service import gerar_programacao_do_dia

    try:
        versao = versao_vigente(dia)
    except (VersaoGTFSInexistente, VersaoGTFSAmbigua) as erro:
        return {"situacao": type(erro).__name__}
    if ImportacaoProgramacao.query.filter_by(versao_gtfs_id=versao.id, data_operacao=dia, fonte="GTFS").first():
        return {"situacao": "JA_GERADA"}
    return gerar_programacao_do_dia(dia, versao_gtfs_id=versao.id)


def corresponder_pendentes(maximo=MAXIMO_CORRESPONDENCIA):
    """MatchingService (US03) sobre os brutos ainda sem observação tratada processada."""
    from app.correspondencia.geografia import Geografia
    from app.correspondencia.repository import CorrespondenciaLoteRepository
    from app.correspondencia.service import MatchingService

    caminho = current_app.config.get("MUNICIPIO_GEOJSON")
    if not caminho:
        return {"situacao": "SEM_LIMITE_MUNICIPAL", "processados": 0}
    if caminho not in _geografia:
        _geografia[caminho] = Geografia.arquivo(caminho)
    repo = CorrespondenciaLoteRepository()
    servico = MatchingService(_geografia[caminho], repository=repo)
    O, B = ObservacaoTratada, RegistroBrutoGPS
    total, ultimo = 0, 0
    while total < maximo:
        ids = [i for (i,) in db.session.query(B.id).outerjoin(O, O.bruto_id == B.id)
               .filter(B.id > ultimo, or_(O.id.is_(None), O.processada_em.is_(None)))
               .order_by(B.id).limit(min(LOTE_CORRESPONDENCIA, maximo - total)).all()]
        if not ids:
            break
        repo.preparar(ids)
        with db.session.no_autoflush:
            for bruto_id in ids:
                servico.processar(bruto_id)
        repo.confirmar()
        db.session.commit()
        ultimo, total = ids[-1], total + len(ids)
    return {"situacao": "OK", "processados": total}


def reconstruir_recentes(agora):
    """TripReconstructionService (US14-T01C) nas últimas horas; já reconstruídas são puladas."""
    from app.viagens.reconstrucao import reconstruir

    resumo = reconstruir(agora - JANELA_RECONSTRUCAO, agora)
    db.session.commit()
    return resumo


def processar_cadeia(agora):
    hoje = agora.astimezone(FUSO).date()
    resultado = {}
    etapas = (("programacao_ontem", lambda: garantir_programacao(hoje - timedelta(days=1))),
              ("programacao_hoje", lambda: garantir_programacao(hoje)),
              ("correspondencia", corresponder_pendentes),
              ("reconstrucao", lambda: reconstruir_recentes(agora)))
    for nome, etapa in etapas:
        try:
            resultado[nome] = etapa()
        except Exception as erro:  # registrada; as outras etapas seguem
            db.session.rollback()
            resultado[nome] = {"situacao": "ERRO", "erro": type(erro).__name__}
            logger.exception("Etapa %s da cadeia falhou", nome)
    logger.info(json.dumps({"evento": "cadeia_processada", **resultado}, ensure_ascii=False, default=str))
    return resultado
