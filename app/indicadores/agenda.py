"""US23-T03: indicadores atuais a cada 15 min e consolidação do dia anterior.

Executada pelo processo de tarefas (US01-T04, ``python -m app.tarefas``) como
tarefa periódica ``atualizar_indicadores(agora)``; nunca dentro do HTTP.

- Atualização: a cada janela de 15 min fechada, recalcula essa janela para cada
  linha/sentido/versões com viagens reconstruídas na data operacional:
  cumprimento (US14) e pontualidade (US16), calculados por janela, e
  frequência (US15), regularidade (US17) e espera/excesso (US18), calculados
  pela data operacional inteira e gravados só na janela. Janelas perdidas
  (processo parado) não são recalculadas em rajada; a consolidação do dia as cobre.
- Consolidação: a partir de 04:00 de D+1 (São Paulo), recalcula todas as janelas
  de 15 min do dia civil D, com prazo até 06:00 de D+1. Janelas após 24h de D-1
  (horário GTFS > 24h) caem no dia civil D e entram aqui.
- Conjunto consistente: cada atualização/consolidação grava todos os registros e
  a sua ``ExecucaoProcessamento`` (estado PRONTO) numa única transação. Registros
  anteriores do mesmo escopo (linha, sentido, GTFS, correspondência, indicador e
  regra) que o novo conjunto não reproduz são removidos na mesma transação, para
  que a publicação nunca misture conjuntos antigos e novos. Falha em qualquer
  ponto desfaz tudo; o painel continua com o conjunto anterior.
"""

from datetime import datetime, time, timedelta, timezone
import logging

from sqlalchemy import func
from sqlalchemy.exc import OperationalError, ProgrammingError

from app import db
from app.core.tempo import FUSO
from app.indicadores.cumprimento import (
    VERSAO_ALGORITMO, calcular_indicador_cumprimento, gravar_indicador,
)
from app.indicadores.espera import ESPERA, EXCESSO, TIPO_PUBLICO as ESPERA_PUBLICO, CalculoEspera
from app.indicadores.espera import VERSAO_CALCULO as VERSAO_ESPERA
from app.indicadores.frequencia import (
    OBSERVADA, PROGRAMADA, TIPO_PUBLICO as FREQUENCIA_PUBLICO, VERSAO_CALCULO as VERSAO_FREQUENCIA,
    CalculoFrequencia,
)
from app.indicadores.pontualidade import (
    VERSAO_ALGORITMO_PONTUALIDADE, calcular_indicador_pontualidade,
)
from app.indicadores.regularidade import REGULARIDADE, CalculoRegularidade
from app.indicadores.regularidade import VERSAO_CALCULO as VERSAO_REGULARIDADE
from app.models import (
    ExecucaoProcessamento, ImportacaoProgramacao, IndicadorOperacional, PartidaProgramada,
    RegistroBrutoGPS, ViagemObservada,
)
from app.qualidade.service import FalhasColeta

logger = logging.getLogger(__name__)
UTC = timezone.utc
INTERVALO = timedelta(minutes=15)
INICIO_CONSOLIDACAO = time(4, 0)
PRAZO_CONSOLIDACAO = time(6, 0)
ATUALIZACAO, CONSOLIDACAO = "ATUALIZACAO_INDICADORES", "CONSOLIDACAO_DIA"
# US20-T03: o recálculo concluído pelo worker de jobs (``app.reprocessamento.execucao``)
# também é uma publicação versionada: entra na versão vigente e na exportação por versão.
RECALCULO = "RECALCULO_INDICADOR"
TIPOS_PUBLICACAO = (ATUALIZACAO, CONSOLIDACAO, RECALCULO)
VERSAO_AGENDA = "us23-t03-v2"
# Calculados por janela (US14/US16) e pela data operacional inteira (US15/US17/US18).
CALCULOS = (calcular_indicador_cumprimento, calcular_indicador_pontualidade)
DIARIOS = (CalculoFrequencia, CalculoRegularidade, CalculoEspera)
# (indicador, regra) que cada publicação recalcula e, portanto, substitui.
PUBLICADOS = (
    ("CUMPRIMENTO_VIAGENS", VERSAO_ALGORITMO), ("PONTUALIDADE_VIAGENS", VERSAO_ALGORITMO_PONTUALIDADE),
    (OBSERVADA, VERSAO_FREQUENCIA), (PROGRAMADA, VERSAO_FREQUENCIA),
    (REGULARIDADE, VERSAO_REGULARIDADE), (ESPERA, VERSAO_ESPERA), (EXCESSO, VERSAO_ESPERA),
)
INDICADORES = sorted({t for t, _ in PUBLICADOS})
# Execuções gravadas pela v1 só recalculavam cumprimento e pontualidade.
INDICADORES_V1 = ("CUMPRIMENTO_VIAGENS", "PONTUALIDADE_VIAGENS")
# Tipos públicos agregados da API → indicadores persistidos que os compõem.
COMPOSTOS = {ESPERA_PUBLICO: (ESPERA, EXCESSO), FREQUENCIA_PUBLICO: (OBSERVADA, PROGRAMADA)}
VERSAO_REGRA = "+".join(sorted({v for _, v in PUBLICADOS}))


def _utc(instante):
    if instante.tzinfo is None:
        raise ValueError("o relógio das tarefas deve fornecer instantes com fuso")
    return instante.astimezone(UTC)


def janela_fechada(agora):
    """Última janela de 15 min inteiramente no passado (limites alinhados em UTC e
    em São Paulo, cujo deslocamento é de horas inteiras)."""
    agora = _utc(agora)
    fim = agora.replace(second=0, microsecond=0) - timedelta(minutes=agora.minute % 15)
    return fim - INTERVALO, fim


def _meia_noite(data):
    return datetime.combine(data, time(), FUSO).astimezone(UTC)


def _contextos(datas):
    """(data operacional, linha, sentido, versão GTFS, correspondência, reconstrução)
    com viagens reconstruídas nas datas. Sem viagem não há como afirmar 0%."""
    V, Pt, I = ViagemObservada, PartidaProgramada, ImportacaoProgramacao
    return (
        db.session.query(I.data_operacao, I.linha_codigo, I.sentido, I.versao_gtfs_id,
                         V.versao_correspondencia, V.versao_reconstrucao)
        .join(Pt, Pt.importacao_id == I.id).join(V, V.partida_programada_id == Pt.id)
        .filter(I.data_operacao.in_(sorted(datas)))
        .distinct().order_by(I.data_operacao, I.linha_codigo, I.sentido, I.versao_gtfs_id,
                             V.versao_correspondencia, V.versao_reconstrucao)
        .all()
    )


def _substituir(inicio, fim, escopos, escritas):
    """Remove, na transação da publicação, registros do mesmo escopo e regra dentro de
    [inicio, fim) que o novo conjunto não reproduziu (ex.: captura de programação
    trocada). Sem isso, API e CSV mostrariam conjuntos antigos e novos lado a lado."""
    if not escopos:
        return 0
    I = IndicadorOperacional
    regras = db.or_(*(db.and_(I.indicador == t, I.versao_calculo == v) for t, v in PUBLICADOS))
    removidos = 0
    for linha, sentido, gtfs, correspondencia in sorted(escopos):
        for registro in I.query.filter(
                I.linha_codigo == linha, I.sentido == sentido, I.versao_gtfs_id == gtfs,
                I.versao_correspondencia == correspondencia, regras,
                I.periodo_inicio >= inicio, I.periodo_fim <= fim):
            if registro.chave_resultado not in escritas:
                db.session.delete(registro)
                removidos += 1
    db.session.flush()
    return removidos


def _publicar(tipo, chave, inicio, fim, janelas, datas, agora, extra=None):
    """Calcula e grava o conjunto inteiro numa transação, com sua execução."""
    execucao = ExecucaoProcessamento.query.filter_by(chave_logica=chave).one_or_none()
    if execucao is not None and execucao.estado == "PRONTO":
        return execucao
    tentativas = (execucao.tentativas if execucao else 0) + 1
    try:
        contextos = _contextos(datas)
        escritas, escopos, falhas = set(), set(), []
        coleta = FalhasColeta(agora=agora)

        def gravar(campos):
            gravar_indicador(campos, commit=False, falhas=coleta).publicado_em = agora
            escritas.add(campos["chave_resultado"])

        def falha(linha, sentido, referencia, calculo, erro):  # registrada, não silenciada
            falhas.append({"linha": linha, "sentido": sentido, "inicio": referencia,
                           "calculo": calculo, "erro": str(erro)[:200]})

        for linha, sentido, gtfs, correspondencia, reconstrucao in sorted({c[1:] for c in contextos}):
            escopos.add((linha, sentido, gtfs, correspondencia))
            for a, b in janelas:
                for calcular in CALCULOS:
                    try:
                        campos = calcular(linha=linha, sentido=sentido, inicio=a, fim=b,
                                          versao_gtfs_id=gtfs, versao_correspondencia=correspondencia,
                                          versao_reconstrucao=reconstrucao)
                    except ValueError as erro:  # ex.: grade ambígua
                        falha(linha, sentido, a.isoformat(), calcular.__name__, erro)
                        continue
                    gravar(campos)
        for data, linha, sentido, gtfs, correspondencia, reconstrucao in contextos:
            for Calculo in DIARIOS:
                try:
                    todos = Calculo(linha=linha, sentido=sentido, data_operacao=data, versao_gtfs_id=gtfs,
                                    versao_correspondencia=correspondencia,
                                    versao_reconstrucao=reconstrucao, agora=agora).calcular()
                except ValueError as erro:  # ex.: percurso da linha indisponível
                    falha(linha, sentido, data.isoformat(), Calculo.__name__, erro)
                    continue
                for campos in todos:  # só as janelas desta publicação
                    if inicio <= campos["periodo_inicio"] and campos["periodo_fim"] <= fim:
                        gravar(campos)
        removidos = _substituir(inicio, fim, escopos, escritas)
        registros = len(escritas)
        bruto_maximo = db.session.query(func.coalesce(func.max(RegistroBrutoGPS.id), 0)).scalar()
        if execucao is None:  # criada só agora, já completa, para não ser gravada pela metade
            execucao = ExecucaoProcessamento(chave_logica=chave, criada_em=agora)
            db.session.add(execucao)
        execucao.tipo, execucao.estado = tipo, "PRONTO"
        execucao.periodo_inicio, execucao.periodo_fim = inicio, fim
        execucao.bruto_id_maximo = bruto_maximo
        execucao.versao_base = "viagens_observadas"
        execucao.versao_codigo, execucao.versao_regra = VERSAO_AGENDA, VERSAO_REGRA
        execucao.tentativas = tentativas
        execucao.iniciada_em, execucao.finalizada_em, execucao.erro = agora, agora, None
        execucao.parametros = {"contextos": len(contextos), "janelas": len(janelas),
                               "registros": registros, "substituidos": removidos, "falhas": falhas,
                               "indicadores": INDICADORES,
                               "datas_operacao": [d.isoformat() for d in sorted(datas)],
                               **(extra or {})}
        db.session.flush()
        # US25-T01: o estado publicado é preservado antes do commit; sem arquivo, sem publicação.
        from app.exportacoes.csv import preservar_conjunto

        preservar_conjunto(execucao)
        db.session.commit()  # registros e execução ficam visíveis juntos
        return execucao
    except Exception as erro:
        db.session.rollback()
        _registrar_erro(tipo, chave, inicio, fim, agora, tentativas, erro)
        raise


def _registrar_erro(tipo, chave, inicio, fim, agora, tentativas, erro):
    """Grava só o estado ERRO (sem nenhum registro parcial) em transação própria."""
    execucao = ExecucaoProcessamento.query.filter_by(chave_logica=chave).one_or_none()
    if execucao is None:
        execucao = ExecucaoProcessamento(chave_logica=chave, criada_em=agora, bruto_id_maximo=0)
    execucao.tipo, execucao.estado, execucao.tentativas = tipo, "ERRO", tentativas
    execucao.periodo_inicio, execucao.periodo_fim = inicio, fim
    execucao.bruto_id_maximo = execucao.bruto_id_maximo or 0
    execucao.versao_base = "viagens_observadas"
    execucao.versao_codigo, execucao.versao_regra = VERSAO_AGENDA, VERSAO_REGRA
    execucao.iniciada_em, execucao.finalizada_em = agora, agora
    execucao.erro = type(erro).__name__[:128]
    db.session.add(execucao)
    db.session.commit()
    logger.error('{"evento":"indicadores_%s_falha","tipo_erro":"%s"}', tipo.lower(), type(erro).__name__)


def atualizar_janela(agora):
    """Indicadores atuais: a última janela de 15 min fechada."""
    agora = _utc(agora)
    inicio, fim = janela_fechada(agora)
    data = inicio.astimezone(FUSO).date()
    return _publicar(ATUALIZACAO, f"US23-T03:{ATUALIZACAO}:{inicio.isoformat()}", inicio, fim,
                     [(inicio, fim)], {data - timedelta(days=1), data}, agora)


def consolidar_dia(data, agora):
    """Recalcula as 96 janelas de 15 min do dia operacional ``data``."""
    agora = _utc(agora)
    inicio, fim = _meia_noite(data), _meia_noite(data + timedelta(days=1))
    janelas = []
    a = inicio
    while a < fim:
        janelas.append((a, a + INTERVALO))
        a += INTERVALO
    prazo = datetime.combine(data + timedelta(days=1), PRAZO_CONSOLIDACAO, FUSO).astimezone(UTC)
    return _publicar(CONSOLIDACAO, f"US23-T03:{CONSOLIDACAO}:{data.isoformat()}", inicio, fim,
                     janelas, {data - timedelta(days=1), data}, agora,
                     {"prazo": prazo.isoformat(), "no_prazo": agora <= prazo})


def atualizar_indicadores(agora):
    """Tarefa periódica do executor: idempotente, chamada no máximo a cada 60 s."""
    agora = _utc(agora)
    resultado = {"atualizacao": atualizar_janela(agora).chave_logica}
    local = agora.astimezone(FUSO)
    if local.time() >= INICIO_CONSOLIDACAO:
        dia = local.date() - timedelta(days=1)
        execucao = consolidar_dia(dia, agora)
        resultado["consolidacao"] = execucao.chave_logica
        # O arquivo é publicado somente depois do commit da consolidação. Se a
        # exportação falhar, a execução diária seguinte tenta novamente sem
        # desfazer indicadores já consolidados.
        try:
            from app.exportacoes.csv import publicar_csv_indicadores

            recibo = publicar_csv_indicadores(dia, agora)
            resultado["csv_indicadores"] = {
                "estado": "PUBLICADO", "data": recibo["data"],
                "registros": recibo["registros"], "sha256": recibo["sha256"],
            }
        except Exception as erro:
            logger.exception("Falha ao publicar CSV diário de indicadores (%s)", type(erro).__name__)
            resultado["csv_indicadores"] = {"estado": "PENDENTE", "data": dia.isoformat()}
    return resultado


def ordem_publicacao(execucao):
    """Ordem total das publicações: no mesmo instante, a consolidação vem depois da
    atualização (``atualizar_indicadores`` executa as duas nessa ordem)."""
    return execucao.finalizada_em, execucao.tipo == CONSOLIDACAO, execucao.id


def versao_vigente():
    """US25-T01: versão do conjunto exibido = última publicação PRONTO (agenda ou
    recálculo US20-T03), lida na mesma transação dos dados. ``id`` é o
    ``versao_dados`` da exportação. Como a publicação grava resultados e PRONTO num
    único commit, a referência só troca quando o conjunto novo está inteiro."""
    E = ExecucaoProcessamento
    try:
        ultima = (E.query.filter(E.estado == "PRONTO", E.tipo.in_(TIPOS_PUBLICACAO))
                  .order_by(E.finalizada_em.desc(), (E.tipo == CONSOLIDACAO).desc(), E.id.desc())
                  .first())
    except (OperationalError, ProgrammingError):
        ultima = None
    if ultima is None:
        return None
    return {"id": ultima.id, "tipo": ultima.tipo, "publicada_em": ultima.finalizada_em.isoformat(),
            "periodo_inicio": ultima.periodo_inicio.isoformat(),
            "periodo_fim": ultima.periodo_fim.isoformat()}


def _ultima(tipo):
    return (ExecucaoProcessamento.query.filter_by(tipo=tipo, estado="PRONTO")
            .order_by(ExecucaoProcessamento.periodo_fim.desc(), ExecucaoProcessamento.finalizada_em.desc())
            .first())


def estado_atualizacao():
    """Bloco ``atualizacao`` do contrato de /series (lido na mesma transação dos dados)."""
    iso = lambda v: v.isoformat() if v is not None else None  # noqa: E731
    try:
        ultima, consolidada = _ultima(ATUALIZACAO), _ultima(CONSOLIDACAO)
    except (OperationalError, ProgrammingError):
        # Base sem a migração 0009 (ensaios que criam só algumas tabelas): só leitura,
        # MySQL/SQLite não abortam a transação por tabela inexistente.
        ultima = consolidada = None
    dia = consolidada.periodo_inicio.astimezone(FUSO).date() if consolidada else None
    return {
        "intervalo_minutos": int(INTERVALO.total_seconds() // 60),
        "ultima_atualizacao": None if ultima is None else {
            "janela_inicio": iso(ultima.periodo_inicio), "janela_fim": iso(ultima.periodo_fim),
            "publicada_em": iso(ultima.finalizada_em),
        },
        "proxima_atualizacao_prevista": iso(ultima.periodo_fim + INTERVALO) if ultima else None,
        "consolidacao": {
            "dia": dia.isoformat() if dia else None,
            "consolidado_ate": iso(consolidada.periodo_fim) if consolidada else None,
            "consolidado_em": iso(consolidada.finalizada_em) if consolidada else None,
            "no_prazo": consolidada.parametros.get("no_prazo") if consolidada else None,
            "regra": "dia D consolidado a partir de 04:00 e até 06:00 de D+1 (America/Sao_Paulo)",
        },
    }


def _coberto(a, b, intervalos):
    """``[a, b)`` coberto pela união de ``intervalos`` [(início, fim, consolidado_em)]?
    Devolve a última consolidação usada, ou None."""
    ultimo = None
    for x, y, em in sorted(intervalos):
        if x > a:
            break
        if y > a:
            a, ultimo = y, max(ultimo or em, em)
        if a >= b:
            return ultimo
    return None


def marcar_consolidados(dados, estado):
    """Cada item informa se o seu período pertence a dias consolidados **que
    recalcularam o seu indicador** (agregações de vários dias exigem todos)."""
    if not dados or estado["consolidacao"]["dia"] is None:
        for item in dados:
            item["consolidado"], item["consolidado_em"] = False, None
        return dados
    limites = [(datetime.fromisoformat(d["inicio"]), datetime.fromisoformat(d["fim"])) for d in dados]
    E = ExecucaoProcessamento
    dias = (db.session.query(E.periodo_inicio, E.periodo_fim, E.finalizada_em, E.parametros)
            .filter(E.tipo == CONSOLIDACAO, E.estado == "PRONTO",
                    E.periodo_fim > min(a for a, _ in limites),
                    E.periodo_inicio < max(b for _, b in limites)).all())
    for item, (a, b) in zip(dados, limites):
        tipos = COMPOSTOS.get(item.get("tipo"), (item.get("tipo"),))
        cobertos = [(x, y, em) for x, y, em, p in dias
                    if set(tipos) <= set((p or {}).get("indicadores") or INDICADORES_V1)]
        em = _coberto(a, b, cobertos)
        item["consolidado"] = em is not None
        item["consolidado_em"] = em.isoformat() if em is not None else None
    return dados
