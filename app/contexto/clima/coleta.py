"""ContextService — coleta de clima (US06-T02).

Executada pelo processo de tarefas (`python -m app.tarefas`) como tarefa
periódica `coletar_clima(agora)`; nunca dentro de uma requisição HTTP.

- INMET: catálogo de estações automáticas próximas a São Paulo (diário) e
  observações horárias (a cada hora, relendo as últimas 48 h para receber
  valores publicados com atraso).
- CGE: a cada 15 min, somente se houver acesso. Sem acesso, a tentativa fica
  registrada como INDISPONIVEL e o INMET segue sem bloqueio.
- Repetição: (estação, instante) é único. Uma observação já gravada só é
  alterada para preencher um valor que antes veio nulo.
- Catálogo alterado (estação nova ou com coordenadas diferentes): os vínculos trecho → estação já calculados são recalculados.
"""

import json
import logging
import math
from datetime import timedelta

from flask import current_app
from sqlalchemy import func

from app import db
from app.integrations.clima import (
    INTERVALO_INMET_SEG, VERSAO_FONTE_INMET, VERSAO_FONTE_INMET_ESTACOES,
    CGEClient, FonteClimaIndisponivel, INMETClient,
)
from app.models import ColetaClima, EstacaoMeteorologica, ObservacaoClima, TrechoEstacaoClima, agora_utc

logger = logging.getLogger(__name__)

# Centro de referência para escolher estações do catálogo (Marco Zero, Sé).
CENTRO_SP = (-23.5505, -46.6333)
INTERVALO_INMET = timedelta(hours=1)
INTERVALO_CGE = timedelta(minutes=15)
INTERVALO_CATALOGO = timedelta(days=1)
RELEITURA_INMET = timedelta(hours=48)


def _registrar(evento, nivel=logging.INFO, **campos):
    logger.log(nivel, json.dumps({"evento": evento, **campos}, ensure_ascii=False, default=str))


def _distancia_km(lat1, lon1, lat2, lon2):
    # Só para escolher candidatos do catálogo; o vínculo com trechos usa metros
    # projetados (associacao.py).
    raio = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * raio * math.asin(math.sqrt(a))


def cliente_inmet():
    return INMETClient(current_app.config.get("INMET_API_URL"), current_app.config.get("INMET_TOKEN"))


def cliente_cge():
    return CGEClient(current_app.config.get("CGE_API_URL"))


def sincronizar_estacoes_inmet(cliente=None, agora=None, raio_km=None):
    """Grava ou atualiza as estações automáticas do INMET num raio de São Paulo.

    Devolve (gravadas, alterou): `alterou` indica estação nova ou com
    coordenadas diferentes, o que exige recalcular o vínculo dos trechos.
    """
    cliente = cliente or cliente_inmet()
    agora = agora or agora_utc()
    raio_km = raio_km if raio_km is not None else float(current_app.config["CLIMA_RAIO_CATALOGO_KM"])
    existentes = {e.codigo: e for e in EstacaoMeteorologica.query.filter_by(fonte="INMET")}
    antes = {c: (e.latitude, e.longitude) for c, e in existentes.items()}
    gravadas, alterou = 0, False
    for estacao in cliente.listar_estacoes_automaticas():
        if _distancia_km(float(estacao.latitude), float(estacao.longitude), *CENTRO_SP) > raio_km:
            continue
        registro = existentes.get(estacao.codigo) or EstacaoMeteorologica(
            fonte="INMET", codigo=estacao.codigo)
        alterou = alterou or antes.get(estacao.codigo) != (estacao.latitude, estacao.longitude)
        registro.nome = estacao.nome
        registro.latitude, registro.longitude = estacao.latitude, estacao.longitude
        registro.altitude_m = estacao.altitude_m
        registro.intervalo_seg = INTERVALO_INMET_SEG
        registro.operacao_inicio = estacao.operacao_inicio
        registro.situacao_fonte = estacao.situacao
        registro.versao_fonte = VERSAO_FONTE_INMET_ESTACOES
        registro.atualizada_em = agora
        db.session.add(registro)
        gravadas += 1
    db.session.commit()
    _registrar("clima_estacoes_inmet", estacoes=gravadas, alterou=alterou)
    return gravadas, alterou


def reassociar_versoes(fonte, agora=None):
    """Recalcula o vínculo das versões GTFS que já têm vínculo para a fonte."""
    from app.contexto.clima.associacao import associar_trechos_a_estacoes
    versoes = [v for (v,) in db.session.query(TrechoEstacaoClima.versao_gtfs_id)
               .filter_by(fonte=fonte).distinct()]
    for versao in sorted(versoes):
        resultado = associar_trechos_a_estacoes(versao, fonte=fonte, agora=agora)
        _registrar("clima_vinculo_recalculado", **resultado.como_dict())
    return versoes


def _gravar_observacao(estacao, obs, coleta, agora):
    """Insere uma observação nova ou preenche nulos; devolve True se for nova."""
    existente = ObservacaoClima.query.filter_by(estacao_id=estacao.id, instante=obs.instante).one_or_none()
    if existente is None:
        db.session.add(ObservacaoClima(
            estacao_id=estacao.id, instante=obs.instante, intervalo_seg=estacao.intervalo_seg,
            chuva=obs.chuva_mm, unidade_chuva="mm",
            temperatura=obs.temperatura_c, unidade_temperatura="degC",
            fonte=estacao.fonte, versao_fonte=VERSAO_FONTE_INMET,
            coletada_em=agora, coleta_id=coleta.id,
        ))
        return True
    if existente.chuva is None and obs.chuva_mm is not None:
        existente.chuva = obs.chuva_mm
    if existente.temperatura is None and obs.temperatura_c is not None:
        existente.temperatura = obs.temperatura_c
    return False


def coletar_inmet(prevista_em=None, *, cliente=None, agora=None):
    """Uma coleta INMET: relê as últimas 48 h de cada estação gravada."""
    cliente = cliente or cliente_inmet()
    agora = agora or agora_utc()
    inicio_periodo = agora - RELEITURA_INMET
    coleta = ColetaClima(fonte="INMET", prevista_em=prevista_em or agora, iniciada_em=agora,
                         estado="EM_ANDAMENTO", periodo_inicio=inicio_periodo, periodo_fim=agora)
    db.session.add(coleta)
    db.session.commit()

    estacoes = EstacaoMeteorologica.query.filter_by(fonte="INMET").order_by(
        EstacaoMeteorologica.codigo).all()
    falhas, motivo, descartados = 0, None, 0
    try:
        for estacao in estacoes:
            try:
                observacoes = cliente.observacoes_horarias(
                    estacao.codigo, inicio_periodo.date(), agora.date())
            except FonteClimaIndisponivel as erro:
                falhas, motivo = falhas + 1, str(erro)[:255]
                continue
            for obs in observacoes:
                # A fonte pode listar horas futuras do dia; só o que já ocorreu.
                if obs.instante > agora or obs.instante <= inicio_periodo:
                    continue
                coleta.recebidas += 1
                descartados += len(obs.descartados)
                coleta.novas += int(_gravar_observacao(estacao, obs, coleta, agora))
            db.session.commit()
    except Exception as erro:
        # Erro inesperado (ex.: banco): a tentativa termina como FALHA, não fica
        # EM_ANDAMENTO. O já gravado permanece; a releitura de 48 h completa o resto.
        db.session.rollback()
        coleta.estado, coleta.erro = "FALHA", f"erro inesperado ({type(erro).__name__})"
        coleta.finalizada_em = agora_utc()
        db.session.commit()
        raise

    if not estacoes:
        coleta.estado, coleta.erro = "FALHA", "nenhuma estação INMET cadastrada"
    elif falhas == len(estacoes):
        indisponivel = motivo and "não configurado" in motivo
        coleta.estado, coleta.erro = ("INDISPONIVEL" if indisponivel else "FALHA"), motivo
    elif falhas:
        coleta.estado, coleta.erro = "PARCIAL", motivo
    else:
        coleta.estado = "OK"
    coleta.finalizada_em = agora_utc()
    db.session.commit()
    _registrar("clima_coleta", fonte="INMET", estado=coleta.estado, recebidas=coleta.recebidas,
               novas=coleta.novas, valores_invalidos_descartados=descartados, erro=coleta.erro)
    return coleta


def coletar_cge(prevista_em=None, *, cliente=None, agora=None):
    """Tentativa CGE: sem acesso, registra INDISPONIVEL e não interrompe nada."""
    cliente = cliente or cliente_cge()
    agora = agora or agora_utc()
    coleta = ColetaClima(fonte="CGE", prevista_em=prevista_em or agora, iniciada_em=agora,
                         estado="EM_ANDAMENTO", periodo_inicio=agora - INTERVALO_CGE, periodo_fim=agora)
    db.session.add(coleta)
    try:
        cliente.observacoes(agora - INTERVALO_CGE, agora)
        # Sem formato acordado o cliente nunca chega aqui (ver CGEClient).
        coleta.estado = "OK"
    except FonteClimaIndisponivel as erro:
        coleta.estado, coleta.erro = "INDISPONIVEL", str(erro)[:255]
    coleta.finalizada_em = agora_utc()
    db.session.commit()
    _registrar("clima_coleta", fonte="CGE", estado=coleta.estado, erro=coleta.erro)
    return coleta


def _ultima(fonte):
    return db.session.query(func.max(ColetaClima.prevista_em)).filter_by(fonte=fonte).scalar()


def _proxima(fonte, intervalo, agora):
    """Próximo horário da grade da fonte, sem rajada após o processo parar."""
    ultima = _ultima(fonte)
    grade = agora - (agora - agora.replace(minute=0, second=0, microsecond=0)) % intervalo
    if ultima is None or ultima < grade:
        return grade
    return None


def coletar_clima(agora):
    """Tarefa periódica: cada fonte isolada; a falha de uma não afeta a outra.

    Chamada a cada volta do processo de tarefas (até 60 s); só trabalha quando
    o horário da grade da fonte ainda não tem tentativa registrada.
    """
    prevista = _proxima("INMET", INTERVALO_INMET, agora)
    if prevista is not None:
        atualizado = (db.session.query(func.max(EstacaoMeteorologica.atualizada_em))
                      .filter_by(fonte="INMET").scalar())
        if atualizado is None or atualizado <= agora - INTERVALO_CATALOGO:
            try:
                _, alterou = sincronizar_estacoes_inmet(agora=agora)
                if alterou:
                    reassociar_versoes("INMET", agora=agora)
            except FonteClimaIndisponivel as erro:
                db.session.rollback()
                _registrar("clima_estacoes_inmet_falha", logging.ERROR, erro=str(erro)[:255])
            except Exception as erro:  # Falha do vínculo não impede a coleta.
                db.session.rollback()
                _registrar("clima_vinculo_falha", logging.ERROR, tipo_erro=type(erro).__name__)
        try:
            coletar_inmet(prevista, agora=agora)
        except Exception as erro:  # Nem o INMET impede a tentativa do CGE.
            db.session.rollback()
            _registrar("clima_coleta_inmet_falha", logging.ERROR, tipo_erro=type(erro).__name__)
    prevista = _proxima("CGE", INTERVALO_CGE, agora)
    if prevista is not None:
        try:
            coletar_cge(prevista, agora=agora)
        except Exception as erro:  # O CGE nunca impede o caminho INMET.
            db.session.rollback()
            _registrar("clima_coleta_cge_falha", logging.ERROR, tipo_erro=type(erro).__name__)
