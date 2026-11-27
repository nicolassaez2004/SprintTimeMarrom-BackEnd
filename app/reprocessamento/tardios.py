"""US20-T01A: janelas afetadas pela chegada tardia de uma posição GPS.

A janela afetada vem do **horário do evento** (``horario_gps``), nunca do
recebimento (``coletado_em``). O recebimento só decide se a chegada foi tardia:
existe resultado afetado publicado antes dela (``publicado_em <= coletado_em``).
Não há prazo de aceitação nem política de fechamento definidos (pendência do
PO); por isso nenhum dado é recusado por ser "tarde demais".

Dependências usadas, todas do cálculo existente:

- Passagem (US15-T01): interpolada entre duas posições a no máximo
  ``PASSAGEM_LACUNA_MAXIMA_SEG``. Uma posição nova só cria/altera passagens em
  ``[evento - lacuna, evento + lacuna]``.
- Frequência observada (US15), regularidade (US17) e espera (US18): janela de
  cada passagem dessa faixa. Regularidade e espera atribuem cada intervalo à
  janela da passagem **posterior**; por isso também entra a janela da próxima
  passagem já existente de cada parada do escopo (vizinha real, não "±1").
- Excesso de espera: dependente confirmado da espera (``dependencias``).
- Cumprimento (US14) e pontualidade (US16): janela da partida programada das
  viagens observadas que cobrem o evento. Sem viagem que o cubra, a posição pode
  formar uma viagem nova, cuja partida é desconhecida: usa-se a faixa técnica
  ``[evento - 24 h, evento + lacuna]`` (pendência: duração máxima de viagem).
- Variação de atraso (US19-T01): registros das mesmas viagens; o agregado mensal
  é dependente. O worker atual não executa esses tipos: ficam no plano como
  ``sem_executor``, para não criar pedidos fadados ao erro.
- Frequência programada não usa GPS e nunca é marcada.

Só lê: não altera brutos, tratados nem indicadores.
"""
from datetime import timedelta, timezone

from sqlalchemy import func

from app import db
from app.indicadores.espera import ESPERA
from app.indicadores.frequencia import OBSERVADA
from app.indicadores.propagacao import INDICADOR_VARIACAO
from app.indicadores.regularidade import REGULARIDADE
from app.models import (
    ImportacaoProgramacao,
    IndicadorOperacional,
    ObservacaoTratada,
    PartidaProgramada,
    PassagemEstimada,
    ViagemObservada,
)
from app.reprocessamento.dependencias import incluir_dependentes
from app.viagens.passagens import lacuna_configurada

JANELA = timedelta(minutes=15)
FAIXA_SEM_VIAGEM = timedelta(hours=24)
POR_PASSAGEM = (OBSERVADA, REGULARIDADE, ESPERA)
POR_INTERVALO = (REGULARIDADE, ESPERA)  # intervalo pertence à janela da passagem posterior
POR_PARTIDA = ("CUMPRIMENTO_VIAGENS", "PONTUALIDADE_VIAGENS")


def _utc(instante):
    if instante is None or instante.tzinfo is None or instante.utcoffset() is None:
        raise ValueError("horário ausente ou sem fuso")
    return instante.astimezone(timezone.utc)


def inicio_janela(instante):
    """Início da janela de 15 min que contém ``instante`` (alinhada em UTC e São Paulo)."""
    instante = _utc(instante)
    return instante.replace(second=0, microsecond=0) - timedelta(minutes=instante.minute % 15)


def janelas_entre(a, b):
    """Inícios das janelas que contêm algum instante de ``[a, b]`` (fechado)."""
    atual, fim = inicio_janela(a), inicio_janela(b)
    janelas = []
    while atual <= fim:
        janelas.append(atual)
        atual += JANELA
    return janelas


def _escopo(bruto):
    tratada = ObservacaoTratada.query.filter_by(bruto_id=bruto.id).one_or_none()
    if (tratada is None or tratada.valida is not True or not tratada.linha_codigo
            or not tratada.versao_gtfs_id or tratada.sentido_gtfs not in ("0", "1")):
        raise ValueError("é necessária correspondência válida com linha, sentido e GTFS")
    return tratada.versao_gtfs_id, tratada.linha_codigo, tratada.sentido_gtfs


def _viagens(gtfs, linha, sentido, a, b):
    """(viagem, saída prevista) das viagens do escopo cujo intervalo observado toca [a, b]."""
    V, P, I = ViagemObservada, PartidaProgramada, ImportacaoProgramacao
    fim = func.coalesce(V.fim_observado, V.saida_observada)
    return (db.session.query(V.id, P.saida_prevista)
            .join(P, P.id == V.partida_programada_id).join(I, I.id == P.importacao_id)
            .filter(I.versao_gtfs_id == gtfs, I.linha_codigo == linha, I.sentido == sentido,
                    V.saida_observada <= b, fim >= a)
            .order_by(V.id).all())


def _proximas_passagens(gtfs, linha, sentido, depois_de):
    """Primeira passagem existente de cada parada do escopo após ``depois_de`` (até 24 h)."""
    Pe, V, P, I = PassagemEstimada, ViagemObservada, PartidaProgramada, ImportacaoProgramacao
    return [instante for _, instante in (
        db.session.query(Pe.parada_codigo, func.min(Pe.instante))
        .join(V, V.id == Pe.viagem_observada_id).join(P, P.id == V.partida_programada_id)
        .join(I, I.id == P.importacao_id)
        .filter(I.versao_gtfs_id == gtfs, I.linha_codigo == linha, I.sentido == sentido,
                Pe.versao_gtfs_id == gtfs, Pe.instante > depois_de,
                Pe.instante <= depois_de + FAIXA_SEM_VIAGEM)
        .group_by(Pe.parada_codigo).all())]


def localizar_janelas_tardias(bruto, *, lacuna=None):
    """Janelas e resultados publicados afetados pela posição ``bruto``; só leitura.

    Retorna escopo, horários (evento e recebimento separados), janelas por
    indicador, resultados executáveis e ``sem_executor``, e se a chegada foi
    tardia. Repetir com a mesma entrada e o mesmo estado do banco dá o mesmo
    resultado.
    """
    evento, recebido = _utc(bruto.horario_gps), _utc(bruto.coletado_em)
    lacuna = lacuna if lacuna is not None else lacuna_configurada()
    if lacuna is None or lacuna <= timedelta(0):
        raise ValueError("lacuna máxima de passagem não definida (PASSAGEM_LACUNA_MAXIMA_SEG)")
    gtfs, linha, sentido = _escopo(bruto)
    a, b = evento - lacuna, evento + lacuna

    janelas = {indicador: set(janelas_entre(a, b)) for indicador in POR_PASSAGEM}
    vizinhas = {inicio_janela(t) for t in _proximas_passagens(gtfs, linha, sentido, b)}
    for indicador in POR_INTERVALO:
        janelas[indicador] |= vizinhas
    viagens = _viagens(gtfs, linha, sentido, a, b)
    if viagens:
        partidas = {inicio_janela(saida) for _, saida in viagens}
        abrangencia_partidas = "PARTIDAS_DAS_VIAGENS_QUE_COBREM_O_EVENTO"
    else:
        partidas = set(janelas_entre(evento - FAIXA_SEM_VIAGEM, b))
        abrangencia_partidas = "SEM_VIAGEM_RECONSTRUIDA_FAIXA_TECNICA_24H"
    for indicador in POR_PARTIDA:
        janelas[indicador] = set(partidas)
    for indicador in list(janelas):  # dependentes confirmados (EXCESSO_ESPERA)
        for dependente in incluir_dependentes({indicador}) - {indicador}:
            janelas.setdefault(dependente, set()).update(janelas[indicador])

    I = IndicadorOperacional
    escopo = I.query.filter(I.versao_gtfs_id == gtfs, I.linha_codigo == linha, I.sentido == sentido)
    resultados = []
    for indicador, inicios in sorted(janelas.items()):
        if inicios:
            resultados += escopo.filter(I.indicador == indicador,
                                        I.periodo_inicio.in_(sorted(inicios))).all()
    # Só janelas de 15 min: agregados de faixa/dia/mês não são gravados por estes tipos.
    resultados = [r for r in resultados if r.periodo_fim - r.periodo_inicio == JANELA]

    ids_viagens = {v for v, _ in viagens}
    sem_executor = []
    if ids_viagens:
        variacoes = [r for r in escopo.filter(I.indicador == INDICADOR_VARIACAO,
                                              I.periodo_inicio <= evento + FAIXA_SEM_VIAGEM,
                                              I.periodo_fim > evento - FAIXA_SEM_VIAGEM)
                     if (r.qualidade or {}).get("viagem_observada_id") in ids_viagens]
        sem_executor += variacoes
        dependentes = incluir_dependentes({INDICADOR_VARIACAO}) - {INDICADOR_VARIACAO}
        for v in variacoes:
            sem_executor += I.query.filter(I.indicador.in_(sorted(dependentes)),
                I.versao_gtfs_id == gtfs, I.trecho_id == v.trecho_id,
                I.periodo_inicio < v.periodo_fim, I.periodo_fim > v.periodo_inicio).all()
    ordem = lambda r: (r.periodo_inicio, r.indicador, r.id)  # noqa: E731
    resultados = sorted({r.id: r for r in resultados}.values(), key=ordem)
    sem_executor = sorted({r.id: r for r in sem_executor}.values(), key=ordem)
    anteriores = [r for r in resultados + sem_executor if r.publicado_em <= recebido]
    return {
        "bruto_id": bruto.id, "versao_gtfs_id": gtfs, "linha_codigo": linha, "sentido": sentido,
        "horario_evento": evento, "recebido_em": recebido,
        "lacuna_passagem_seg": lacuna.total_seconds(),
        "faixa_passagens": (a, b),
        "janelas": {k: sorted(v) for k, v in sorted(janelas.items())},
        "janelas_vizinhas": sorted(vizinhas), "viagens": sorted(ids_viagens),
        "abrangencia_partidas": abrangencia_partidas,
        "resultados": resultados, "sem_executor": sem_executor,
        "tardio": bool(anteriores), "publicados_antes_da_chegada": [r.id for r in anteriores],
    }
