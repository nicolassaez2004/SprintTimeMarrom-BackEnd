"""US14-T01C — TripReconstructionService: ``ViagemObservada`` a partir das observações tratadas.

Diagrama de componentes (fluxo 1): MatchingService → **TripReconstructionService** →
IndicatorService. Só lê ``ObservacaoTratada`` válidas (com linha, sentido e versão GTFS) e os
``RegistroBrutoGPS`` correspondentes; nunca altera brutos nem tratados.

Para cada veículo:

1. ``SeparadorViagens`` (US16-T01) separa as posições em viagens contínuas por veículo,
   linha e sentido (lacuna > 15 min, troca de linha ou de sentido encerram a viagem).
2. Cada posição é medida ao longo do percurso GTFS da linha/sentido (``Percurso``: soma dos
   trechos entre paradas).
3. **Cobertura do traçado**: união dos intervalos percorridos entre posições consecutivas a
   até ``LACUNA_COBERTURA`` uma da outra, dividida pelo comprimento do percurso. Recuo não
   conta como avanço (critério da US14: viagem cumprida cobre ≥ 80% do traçado).
4. **Saída observada**: último instante do veículo a até ``ORIGEM_M`` do início do percurso
   antes de avançar além dele. Sem posição na origem (viagem que começa fora dela), vale a
   primeira posição, e ``qualidade.saida`` registra isso.
5. **Partida programada**: a de horário mais próximo da saída, a até ``JANELA_ASSOCIACAO``,
   da mesma versão GTFS, linha e sentido; a fonte OFICIAL tem prioridade sobre a gerada do
   GTFS. Associação um a um, gulosa pela menor diferença; cada partida recebe no máximo uma
   viagem por ``VERSAO_RECONSTRUCAO`` (restrição única do modelo).
6. Grava a ``ViagemObservada`` e estima as passagens nas paradas (US15-T01,
   ``estimar_e_gravar``).

Repetir sobre o mesmo período não duplica: viagens já reconstruídas (mesmo veículo e saída) e
partidas já associadas são puladas. Viagens que ainda podem continuar (terminam perto do fim do
período consultado) ficam para a execução seguinte.
"""
from collections import defaultdict
from datetime import timedelta
import logging

from sqlalchemy.orm import contains_eager

from app import db
from app.models import (
    ImportacaoProgramacao, ObservacaoTratada, PartidaProgramada, RegistroBrutoGPS, ViagemObservada,
)
from app.viagens.passagens import Percurso, estimar_e_gravar
from app.viagens.separacao import ObservacaoViagem, SeparadorViagens

logger = logging.getLogger(__name__)

VERSAO_RECONSTRUCAO = "reconstrucao-1.0"
ORIGEM_M = 300.0
LACUNA_COBERTURA = timedelta(minutes=5)
LACUNA_VIAGEM = timedelta(minutes=15)
JANELA_ASSOCIACAO = timedelta(minutes=60)
MINIMO_POSICOES = 3


def comprimento(percurso):
    return percurso.inicios[-1] + percurso.linhas[-1].length


def cobertura(pontos, total):
    """``pontos``: [(medida_m, instante)] em ordem de tempo. Fração do percurso coberta."""
    if total <= 0:
        return 0.0
    intervalos = sorted((m1, m2) for (m1, t1), (m2, t2) in zip(pontos, pontos[1:])
                        if m2 > m1 and t2 - t1 <= LACUNA_COBERTURA)
    coberto, inicio, fim = 0.0, None, None
    for a, b in intervalos:
        if inicio is None or a > fim:
            if inicio is not None:
                coberto += fim - inicio
            inicio, fim = a, b
        else:
            fim = max(fim, b)
    if inicio is not None:
        coberto += fim - inicio
    return max(0.0, min(1.0, coberto / total))


def saida(pontos):
    """(instante, criterio) da saída da origem; None se o veículo não saiu da origem."""
    na_origem = None
    for medida, instante in pontos:
        if medida <= ORIGEM_M:
            na_origem = instante
        elif na_origem is not None:
            return na_origem, "ULTIMA_POSICAO_NA_ORIGEM"
        else:
            return pontos[0][1], "PRIMEIRA_POSICAO_FORA_DA_ORIGEM"
    return None  # todas as posições na origem: ainda não partiu


def _observacoes(inicio, fim):
    O, B = ObservacaoTratada, RegistroBrutoGPS
    return (O.query.join(B, B.id == O.bruto_id).options(contains_eager(O.bruto))
            .filter(O.valida.is_(True), O.linha_codigo.isnot(None), O.versao_gtfs_id.isnot(None),
                    O.sentido_gtfs.in_(("0", "1")), B.prefixo_origem.isnot(None), B.horario_gps.isnot(None),
                    B.horario_gps >= inicio, B.horario_gps < fim)
            .order_by(B.prefixo_origem, B.horario_gps, B.id).all())


def _candidatas(versao_gtfs_id, linha, sentido, instante):
    P, I = PartidaProgramada, ImportacaoProgramacao
    return (db.session.query(P.id, P.saida_prevista, I.fonte).join(I, I.id == P.importacao_id)
            .filter(I.versao_gtfs_id == versao_gtfs_id, I.linha_codigo == linha, I.sentido == sentido,
                    P.saida_prevista >= instante - JANELA_ASSOCIACAO,
                    P.saida_prevista <= instante + JANELA_ASSOCIACAO).all())


def reconstruir(inicio, fim, *, versao=VERSAO_RECONSTRUCAO):
    """Reconstrói as viagens com posições em [inicio, fim). Grava sem commit; devolve o resumo."""
    observacoes = _observacoes(inicio - LACUNA_VIAGEM, fim)
    por_chave = defaultdict(list)
    for o in observacoes:
        por_chave[(o.bruto.prefixo_origem, o.versao_gtfs_id)].append(o)

    resumo = defaultdict(int)
    resumo["posicoes"] = len(observacoes)
    existentes = {((v.qualidade or {}).get("prefixo"), v.saida_observada)
                  for v in ViagemObservada.query.filter(
                      ViagemObservada.versao_reconstrucao == versao,
                      ViagemObservada.saida_observada >= inicio - JANELA_ASSOCIACAO,
                      ViagemObservada.saida_observada < fim + JANELA_ASSOCIACAO)}
    percursos, viagens = {}, []
    separador = SeparadorViagens(LACUNA_VIAGEM)
    for (prefixo, versao_gtfs), lista in por_chave.items():
        separacao = separador.separar(ObservacaoViagem(prefixo, o.linha_codigo, o.sentido_gtfs,
                                                       o.bruto.horario_gps, True) for o in lista)
        for trecho in separacao.viagens:
            if trecho.motivo_encerramento == "fim_da_amostra" and trecho.fim > fim - LACUNA_VIAGEM:
                resumo["em_andamento"] += 1
                continue  # pode continuar: fica para a próxima execução
            if trecho.inicio < inicio:
                resumo["ja_processadas_no_periodo_anterior"] += 1
                continue
            posicoes = [o for o in lista if o.linha_codigo == trecho.linha_codigo
                        and o.sentido_gtfs == trecho.sentido_gtfs
                        and trecho.inicio <= o.bruto.horario_gps <= trecho.fim]
            if len(posicoes) < MINIMO_POSICOES:
                resumo["poucas_posicoes"] += 1
                continue
            chave_percurso = (versao_gtfs, trecho.linha_codigo, trecho.sentido_gtfs)
            if chave_percurso not in percursos:
                percursos[chave_percurso] = Percurso.da_rede(*chave_percurso)
            percurso, motivo = percursos[chave_percurso]
            if percurso is None:
                resumo[f"sem_percurso_{motivo}"] += 1
                continue
            pontos = [(percurso.medida(o.bruto.longitude, o.bruto.latitude), o.bruto.horario_gps)
                      for o in posicoes if o.bruto.latitude is not None and o.bruto.longitude is not None]
            achada = saida(pontos) if len(pontos) >= MINIMO_POSICOES else None
            if achada is None:
                resumo["sem_deslocamento"] += 1
                continue
            instante_saida, criterio = achada
            if (prefixo, instante_saida) in existentes:
                resumo["ja_reconstruidas"] += 1
                continue
            viagens.append({
                "prefixo": prefixo, "versao_gtfs_id": versao_gtfs, "linha": trecho.linha_codigo,
                "sentido": trecho.sentido_gtfs, "saida": instante_saida, "criterio_saida": criterio,
                "fim": trecho.fim, "motivo_fim": trecho.motivo_encerramento,
                "cobertura": cobertura(pontos, comprimento(percurso)),
                "versao_correspondencia": posicoes[0].versao_correspondencia,
                # Passagens a partir da saída: o tempo parado no terminal não é percurso.
                "posicoes": [o for o in posicoes if instante_saida <= o.bruto.horario_gps <= trecho.fim],
            })

    # Associação um a um, gulosa pela menor diferença (fonte OFICIAL antes da gerada do GTFS).
    usadas = {pid for (pid,) in db.session.query(ViagemObservada.partida_programada_id).filter(
        ViagemObservada.versao_reconstrucao == versao,
        ViagemObservada.saida_observada >= inicio - 2 * JANELA_ASSOCIACAO,
        ViagemObservada.saida_observada < fim + 2 * JANELA_ASSOCIACAO)}
    pares = []
    for i, v in enumerate(viagens):
        for pid, prevista, fonte in _candidatas(v["versao_gtfs_id"], v["linha"], v["sentido"], v["saida"]):
            pares.append((fonte != "OFICIAL", abs((v["saida"] - prevista).total_seconds()), i, pid, prevista))
    associadas = {}
    for _, diferenca, i, pid, prevista in sorted(pares):
        if i in associadas or pid in usadas:
            continue
        associadas[i] = (pid, prevista)
        usadas.add(pid)

    for i, v in enumerate(viagens):
        if i not in associadas:
            resumo["sem_partida_programada"] += 1
            continue
        pid, prevista = associadas[i]
        viagem = ViagemObservada(
            partida_programada_id=pid, saida_observada=v["saida"], fim_observado=v["fim"],
            cobertura_tracado=round(v["cobertura"], 6), versao_reconstrucao=versao,
            versao_correspondencia=v["versao_correspondencia"],
            qualidade={"prefixo": v["prefixo"], "posicoes": len(v["posicoes"]), "saida": v["criterio_saida"],
                       "fim": v["motivo_fim"], "diferenca_partida_seg": (v["saida"] - prevista).total_seconds(),
                       "metodo_cobertura": f"uniao de intervalos com lacuna <= {int(LACUNA_COBERTURA.total_seconds())} s",
                       "origem_m": ORIGEM_M})
        db.session.add(viagem)
        db.session.flush()
        try:
            estimar_e_gravar(viagem, v["posicoes"])
            resumo["passagens_estimadas"] += 1
        except ValueError as erro:  # ex.: posições incompatíveis com o percurso
            resumo["passagens_nao_estimadas"] += 1
            logger.warning("Passagens não estimadas para a viagem %s: %s", viagem.id, erro)
        resumo["viagens"] += 1
    return dict(resumo)
