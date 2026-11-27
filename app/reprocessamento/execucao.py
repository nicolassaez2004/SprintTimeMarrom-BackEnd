"""US20-T02B: executa um pedido ``RECALCULO_INDICADOR`` com os cálculos existentes.

Chamado pelo worker (``app.jobs.executor``) depois da reserva. Usa as mesmas
funções e contextos da agenda US23-T03 (US14-US18) para o indicador, escopo e
janela gravados no pedido, e grava por ``gravar_indicador`` **sem commit**: o
executor confirma resultados e PRONTO juntos, só se ainda tiver a reserva.

Não altera brutos nem o pedido (chave, versões, escopo e parâmetros).

US20-T02C: ``calcular_pedido`` (só leituras) e ``publicar_pedido`` (upsert pela
``chave_resultado``) são as duas fases do worker, que valida a posse entre elas.

Revisão US20: a publicação não sobrescreve um registro publicado depois de esta
tentativa começar a ler os dados, nem um gravado por geração mais nova do mesmo
pedido; cada registro gravado guarda ``qualidade.recalculo`` (pedido, chave,
snapshot e versões); e registros do mesmo escopo, janela e regra que o novo
conjunto não reproduz são removidos na mesma transação (como ``agenda._substituir``).

US20-T03: o recálculo é uma publicação versionada. ``preservar_publicacao`` guarda o
conjunto do período (US25) antes do commit que grava resultados e PRONTO juntos; só
então ``versao_vigente`` passa a apontar o pedido. Leitores veem o conjunto antigo
ou o novo, nunca um parcial; em falha, o rollback mantém o antigo. Depois do commit,
``invalidar_caches`` regera o CSV diário consolidado dos dias alterados.
``executor_recalculo`` monta o worker com essas etapas.
"""
from datetime import datetime, timedelta, timezone

from app import db
from app.core.tempo import FUSO
from app.indicadores.agenda import CONSOLIDACAO, PUBLICADOS, RECALCULO, _contextos
from app.indicadores.cumprimento import calcular_indicador_cumprimento, gravar_indicador
from app.indicadores.espera import ESPERA, EXCESSO, CalculoEspera
from app.indicadores.frequencia import OBSERVADA, PROGRAMADA, CalculoFrequencia
from app.indicadores.pontualidade import calcular_indicador_pontualidade
from app.indicadores.regularidade import REGULARIDADE, CalculoRegularidade
from app.models import ExecucaoProcessamento, IndicadorOperacional
from app.qualidade.service import FalhasColeta

REGRA_VIGENTE = dict(PUBLICADOS)  # indicador -> versão da regra implementada no código
POR_JANELA = {"CUMPRIMENTO_VIAGENS": calcular_indicador_cumprimento,
              "PONTUALIDADE_VIAGENS": calcular_indicador_pontualidade}
POR_DIA = {OBSERVADA: CalculoFrequencia, PROGRAMADA: CalculoFrequencia,
           REGULARIDADE: CalculoRegularidade, ESPERA: CalculoEspera, EXCESSO: CalculoEspera}


class RegraIndisponivel(ValueError):
    """O código em execução não implementa a versão de regra pedida."""


def _no_escopo(campos, pedido):
    escopo = pedido.parametros["escopo"]
    return (campos["indicador"] == pedido.parametros["indicador"]
            and pedido.periodo_inicio <= campos["periodo_inicio"]
            and campos["periodo_fim"] <= pedido.periodo_fim
            and campos.get("parada_codigo") == escopo.get("parada")
            and campos.get("trecho_id") == escopo.get("trecho"))


def calcular_pedido(pedido, agora=None):
    """Calcula, sem gravar, os resultados do indicador, escopo e janela do pedido."""
    agora = agora or datetime.now(timezone.utc)
    indicador, escopo = pedido.parametros["indicador"], pedido.parametros["escopo"]
    if REGRA_VIGENTE.get(indicador) != pedido.versao_regra:
        raise RegraIndisponivel(f"{indicador} {pedido.versao_regra}")
    dia = pedido.periodo_inicio.astimezone(FUSO).date()
    contextos = [c for c in _contextos({dia - timedelta(days=1), dia})
                 if c[1] == escopo["linha"] and c[2] == escopo["sentido"]
                 and escopo.get("gtfs") in (None, c[3])]
    if indicador in POR_JANELA:
        calcular = POR_JANELA[indicador]
        resultados = [calcular(linha=linha, sentido=sentido, inicio=pedido.periodo_inicio,
                               fim=pedido.periodo_fim, versao_gtfs_id=gtfs,
                               versao_correspondencia=corresp, versao_reconstrucao=recon)
                      for linha, sentido, gtfs, corresp, recon in sorted({c[1:] for c in contextos})]
    else:
        Calculo = POR_DIA[indicador]
        resultados = [campos for data, linha, sentido, gtfs, corresp, recon in contextos
                      for campos in Calculo(linha=linha, sentido=sentido, data_operacao=data,
                                            versao_gtfs_id=gtfs, versao_correspondencia=corresp,
                                            versao_reconstrucao=recon, agora=agora).calcular()]
    return [campos for campos in resultados if _no_escopo(campos, pedido)]


def _rastro(pedido):
    origem = (pedido.parametros or {}).get("origem") or {}
    return {"pedido_id": pedido.id, "chave_logica": pedido.chave_logica,
            "snapshot_bruto_id_maximo": pedido.bruto_id_maximo, "versao_base": pedido.versao_base,
            "versao_regra": pedido.versao_regra, "versao_codigo": pedido.versao_codigo,
            "plano_id": origem.get("plano_id"), "origem": origem.get("tipo")}


def _mais_recente(existente, pedido):
    """O registro atual veio de publicação posterior ao início desta leitura?"""
    if existente is None:
        return False
    anterior = (existente.qualidade or {}).get("recalculo") or {}
    if (anterior.get("snapshot_bruto_id_maximo") or -1) > pedido.bruto_id_maximo:
        return True
    return pedido.iniciada_em is not None and existente.publicado_em > pedido.iniciada_em


def _substituir_no_escopo(pedido, gravados, escritas):
    """Remove registros do mesmo indicador, regra, escopo e janela não reproduzidos,
    e os registros que o pedido recalcula (``resultados_ids``)."""
    if not gravados:
        return 0
    I, escopo = IndicadorOperacional, pedido.parametros["escopo"]
    removidos = 0
    for gtfs, linha, sentido, correspondencia in sorted(
            {(c["versao_gtfs_id"], c["linha_codigo"], c["sentido"], c["versao_correspondencia"])
             for c in gravados}, key=str):
        for registro in I.query.filter(
                I.indicador == pedido.parametros["indicador"], I.versao_calculo == pedido.versao_regra,
                I.versao_gtfs_id == gtfs, I.linha_codigo == linha, I.sentido == sentido,
                I.versao_correspondencia == correspondencia,
                I.parada_codigo == escopo.get("parada") if escopo.get("parada") else I.parada_codigo.is_(None),
                I.trecho_id == escopo.get("trecho") if escopo.get("trecho") else I.trecho_id.is_(None),
                I.periodo_inicio >= pedido.periodo_inicio, I.periodo_fim <= pedido.periodo_fim):
            if registro.chave_resultado not in escritas and not _mais_recente(registro, pedido):
                db.session.delete(registro)
                removidos += 1
    # US20-T04: os registros que motivaram o pedido (``resultados_ids``) também são
    # substituídos. Numa mudança de regra eles têm outra ``versao_calculo`` (e outra
    # chave); sem isso o painel mostraria a regra antiga ao lado da nova. A versão
    # anterior continua preservada no arquivo da sua publicação (US25).
    ids = (pedido.parametros or {}).get("resultados_ids") or []
    for registro in I.query.filter(I.id.in_(ids)).all() if ids else ():
        if registro.chave_resultado not in escritas and not _mais_recente(registro, pedido):
            db.session.delete(registro)
            removidos += 1
    db.session.flush()
    return removidos


def publicar_pedido(pedido, resultados, agora=None):
    """Grava (sem commit) os resultados; repetir não duplica (upsert por ``chave_resultado``).

    Retorna quantos registros foram gravados (os preservados por serem mais
    recentes não contam).
    """
    agora = agora or datetime.now(timezone.utc)
    falhas, rastro = FalhasColeta(agora=agora), _rastro(pedido)
    gravados, escritas = [], set()
    for campos in resultados:
        escritas.add(campos["chave_resultado"])
        existente = (IndicadorOperacional.query.filter_by(chave_resultado=campos["chave_resultado"])
                     .with_for_update().one_or_none())
        if _mais_recente(existente, pedido):
            continue  # uma publicação posterior já refletiu dados mais novos
        campos = {**campos, "qualidade": {**(campos.get("qualidade") or {}), "recalculo": rastro}}
        gravar_indicador(campos, commit=False, falhas=falhas).publicado_em = agora
        gravados.append(campos)
    _substituir_no_escopo(pedido, gravados, escritas)
    return len(gravados)


def recalcular_pedido(pedido, agora=None):
    """Calcula e grava (sem commit) numa só fase; retorna quantos registros."""
    return publicar_pedido(pedido, calcular_pedido(pedido, agora), agora)


def preservar_publicacao(pedido_id):
    """US20-T03: chamado pelo executor depois de ``concluir`` e ANTES do commit."""
    from app.exportacoes.csv import preservar_conjunto

    preservar_conjunto(db.session.get(ExecucaoProcessamento, pedido_id, populate_existing=True))


def dias_afetados(pedido):
    """Dias de São Paulo tocados por ``[periodo_inicio, periodo_fim)``."""
    dia = pedido.periodo_inicio.astimezone(FUSO).date()
    ultimo = (pedido.periodo_fim - timedelta(microseconds=1)).astimezone(FUSO).date()
    dias = []
    while dia <= ultimo:
        dias.append(dia)
        dia += timedelta(days=1)
    return dias


def invalidar_caches(pedido_id, agora=None):
    """US20-T03, depois do commit: regera o CSV diário já consolidado de cada dia
    alterado (o antigo deixa de ser servido assim que o recálculo é confirmado).
    Dias ainda não consolidados não têm arquivo; a consolidação usará os dados novos."""
    from app.exportacoes.csv import publicar_csv_indicadores

    pedido = db.session.get(ExecucaoProcessamento, pedido_id, populate_existing=True)
    agora = max(agora or datetime.now(timezone.utc), pedido.finalizada_em)
    regerados = []
    for dia in dias_afetados(pedido):
        chave = f"US23-T03:{CONSOLIDACAO}:{dia.isoformat()}"
        if ExecucaoProcessamento.query.filter_by(chave_logica=chave, estado="PRONTO").first():
            publicar_csv_indicadores(dia, agora)
            regerados.append(dia)
    return regerados


def executor_recalculo(app, **opcoes):
    """Worker de ``RECALCULO_INDICADOR`` com cálculo, publicação, versão e caches."""
    from app.jobs.executor import Executor

    return Executor(app, RECALCULO, calcular_pedido, publicar_pedido,
                    preservar=preservar_publicacao, apos_confirmar=invalidar_caches, **opcoes)
