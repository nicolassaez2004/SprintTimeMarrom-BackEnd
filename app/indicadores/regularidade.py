"""US17: regularidade dos intervalos entre passagens da oferta por frequência.

Regras (docs/US17.md):

- T01, intervalos: passagens estimadas (US15-T01) da mesma linha, sentido,
  parada e data operacional, **somente de viagens cujas partidas são da oferta
  por frequência** (``tipo_oferta = FREQUENCIA``). A mesma evidência GPS (parada
  + par de posições) conta uma vez, como na US15-T02, antes de ordenar e
  calcular as diferenças sucessivas. Linhas, sentidos e paradas nunca se
  misturam.
- Fronteira de janela: cada intervalo pertence à janela ``[início, fim)`` da sua
  passagem **posterior**; a anterior pode estar na janela anterior. Assim a
  união das janelas reproduz exatamente os intervalos do período maior.
- T02, regularidade: ``CV = desvio padrão populacional (divisor n) ÷ média``
  dos intervalos, em segundos. NULL com menos de três passagens (dois
  intervalos) ou média inválida (zero). Agrupamento só quando ``CV > 0,5``;
  ``CV = 0,5`` não é agrupamento. Insuficiência nunca vira CV 0.
- Confiabilidade: a mesma da frequência (US15-T02). Um intervalo só entra se a
  coleta (US13) for confiável em todo o seu trecho e a parada não tiver
  passagem sem evidência GPS nele; caso contrário a janela fica NULL com motivo.
"""

from datetime import timedelta
import json
from statistics import fmean, pstdev

from app import db
from app.indicadores.cumprimento import gravar_indicador
from app.indicadores.frequencia import (
    COLETA_CONFIAVEL, CalculoFrequencia, PublicacaoFrequencia, _hash, _horas, _lacunas,
)
from app.qualidade.service import JANELA, FalhasColeta
from app.repositories.programacao import consultar_por_dia
from app.viagens.passagens import Percurso

REGULARIDADE = "REGULARIDADE_INTERVALOS"
VERSAO_CALCULO = "regularidade-1.0"
UNIDADE = "CV (adimensional)"
OFERTA = "FREQUENCIA"
LIMITE_AGRUPAMENTO = 0.5
MINIMO_PASSAGENS = 3
CASAS = 6  # o CV é publicado com 6 casas; o agrupamento é decidido sobre esse valor
CONVENCAO = ("CV = desvio padrão populacional (divisor n) ÷ média dos intervalos entre "
             "passagens sucessivas, em segundos")
REGRA_FRONTEIRA = "cada intervalo pertence à janela [início, fim) da sua passagem posterior"
REGRA_AGREGACAO = ("CV recalculado sobre a união dos intervalos das janelas; NULL se houver "
                   "janela não confiável, lacuna de publicação ou janelas sobrepostas")
# Motivos que só dizem respeito à janela isolada: na agregação os intervalos dela contam.
INSUFICIENCIA = frozenset({"passagens_insuficientes", "media_invalida"})


def intervalos_sucessivos(instantes):
    """[(anterior, posterior, segundos)] em ordem; instantes iguais dão intervalo 0."""
    ordenados = sorted(instantes)
    return [(a, b, (b - a).total_seconds()) for a, b in zip(ordenados, ordenados[1:])]


def coeficiente_variacao(intervalos_seg):
    """``(cv, motivo)``: NULL com menos de 3 passagens ou média inválida."""
    if len(intervalos_seg) < MINIMO_PASSAGENS - 1:
        return None, "passagens_insuficientes"
    media = fmean(intervalos_seg)
    if not media > 0:
        return None, "media_invalida"
    return pstdev(intervalos_seg) / media, None


def publicado(cv):
    return None if cv is None else round(cv, CASAS)


def agrupamento(cv):
    """``CV > 0,5`` sobre o valor publicado: um CV 0,5 com erro de ponto flutuante
    (0,5000000000000001, comum com intervalos fracionários) não é agrupamento."""
    return None if cv is None else publicado(cv) > LIMITE_AGRUPAMENTO


def _inicio_15min(instante):
    return instante - timedelta(minutes=instante.minute % 15, seconds=instante.second,
                                microseconds=instante.microsecond)


class CalculoRegularidade(CalculoFrequencia):
    """Uma linha/sentido/data operacional; reaproveita passagens, pendências,
    janelas e coleta da US15-T02 (nenhum algoritmo repetido)."""

    def _oferta(self):
        importacao, _ = consultar_por_dia(self.versao_gtfs_id, self.linha, self.sentido, self.data)
        return {p.tipo_oferta for p in importacao.partidas} if importacao else set()

    def _motivos_trecho(self, a, b, parada, coleta, pendencias):
        """Motivos que impedem confiar no trecho [a, b] da parada."""
        motivos = set()
        t = _inicio_15min(a)
        while t <= b:
            janela = coleta.get(t.isoformat())
            situacao = janela["situacao"] if janela else "FORA_DO_RELATORIO"
            if situacao not in COLETA_CONFIAVEL:
                motivos.add("coleta_" + situacao.lower())
            t += JANELA
        for x, y, motivo in [*pendencias.get(None, ()), *pendencias.get(parada, ())]:
            if x <= b and (y is None or y >= a):
                motivos.add(motivo)
        return motivos

    def janelas_com_intervalos(self, indicador=REGULARIDADE, versao_calculo=VERSAO_CALCULO):
        """(início, fim, parada, intervalos [(anterior, posterior, s)], bloqueios) por janela.

        Base compartilhada com a US18 (espera), que reaproveita os mesmos intervalos."""
        percurso, motivo = Percurso.da_rede(self.versao_gtfs_id, self.linha, self.sentido)
        if percurso is None:
            raise ValueError(f"paradas da linha/sentido indisponíveis: {motivo}")
        paradas = list(dict.fromkeys(codigo for codigo, _ in percurso.paradas))
        captura, partidas, motivo_programacao = self._programacao()
        estimativa = self.versoes["versao_estimativa"]
        passagens = self._passagens(tipo_oferta=OFERTA) if estimativa else {}
        pendencias = self._pendencias() if estimativa else {}
        janelas = self._janelas(partidas + [t for ts in passagens.values() for t in ts])
        coleta = self._coleta(janelas)
        sem_frequencia = not motivo_programacao and OFERTA not in self._oferta()
        self.captura = captura
        contexto = {"indicador": indicador, "linha": self.linha, "sentido": self.sentido,
                    "data_operacao": self.data.isoformat(), "versao_gtfs_id": self.versao_gtfs_id,
                    "versao_calculo": versao_calculo, "janela_minutos": int(self.janela.total_seconds() // 60),
                    **self.versoes, "captura_programacao": captura}
        self.versao_conjunto = _hash(contexto)[:32]
        intervalos = {p: intervalos_sucessivos(passagens.get(p, ())) for p in paradas}

        for inicio, fim in janelas:
            for parada in paradas:
                da_janela = [i for i in intervalos[parada] if inicio <= i[1] < fim]
                bloqueio = (
                    {"lacuna_maxima_indefinida"} if not estimativa else
                    {motivo_programacao} if motivo_programacao else
                    {"oferta_sem_frequencia"} if sem_frequencia else
                    self._motivos_trecho(min([inicio, *(a for a, _, _ in da_janela)]),
                                         fim - timedelta(microseconds=1), parada, coleta, pendencias)
                )
                yield inicio, fim, parada, da_janela, sorted(bloqueio)

    def calcular(self):
        """Campos de ``IndicadorOperacional`` por janela e parada, sem gravar."""
        return [self._campos(parada, inicio, fim, [s for _, _, s in da_janela], bloqueio, self.captura)
                for inicio, fim, parada, da_janela, bloqueio in self.janelas_com_intervalos()]

    def _campos(self, parada, inicio, fim, intervalos_seg, bloqueio, captura):
        if bloqueio:
            cv, motivo, situacao = None, "_".join(bloqueio), "DADOS_AUSENTES"
        else:
            cv, motivo = coeficiente_variacao(intervalos_seg)
            situacao = "CALCULADA" if cv is not None else "DADOS_INSUFICIENTES"
        chave = _hash({"indicador": REGULARIDADE, "parada": parada, "inicio": inicio.isoformat(),
                       "fim": fim.isoformat(), "conjunto": self.versao_conjunto})
        return {
            "chave_resultado": chave, "versao_gtfs_id": self.versao_gtfs_id,
            "linha_codigo": self.linha, "sentido": self.sentido, "parada_codigo": parada,
            "trecho_id": None, "indicador": REGULARIDADE,
            "valor": publicado(cv), "unidade": UNIDADE,
            "periodo_inicio": inicio, "periodo_fim": fim,
            "contagem_numerador": None if bloqueio else len(intervalos_seg),
            "contagem_denominador": None,
            "versao_conjunto": self.versao_conjunto,
            "versao_correspondencia": self.versoes["versao_correspondencia"],
            "versao_calculo": VERSAO_CALCULO,
            "qualidade": {
                "situacao": situacao, "motivo": motivo, "agrupamento": agrupamento(cv),
                "limite_agrupamento": LIMITE_AGRUPAMENTO, "minimo_passagens": MINIMO_PASSAGENS,
                "intervalos_seg": [] if bloqueio else intervalos_seg,
                "media_intervalo_seg": fmean(intervalos_seg) if intervalos_seg and not bloqueio else None,
                "desvio_padrao_seg": pstdev(intervalos_seg) if intervalos_seg and not bloqueio else None,
                "convencao": CONVENCAO, "regra_fronteira": REGRA_FRONTEIRA, "oferta": OFERTA,
                "duracao_horas": _horas(inicio, fim), "data_operacao": self.data.isoformat(),
                "captura_programacao": captura, **self.versoes,
            },
        }


def calcular_regularidade(**parametros):
    return CalculoRegularidade(**parametros).calcular()


def calcular_e_publicar_regularidade(**parametros):
    """Calcula e grava o conjunto numa única transação (upsert por chave)."""
    try:
        falhas = FalhasColeta()
        registros = [gravar_indicador(c, commit=False, falhas=falhas)
                     for c in calcular_regularidade(**parametros)]
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return registros


# --- US17-T03A: consulta e agregação (mesmos baldes da US15-T03) ---------------

class PublicacaoRegularidade(PublicacaoFrequencia):
    """Recalcula o CV pela união dos intervalos publicados de cada balde."""

    INDICADORES = (REGULARIDADE,)

    def _item(self, chave, registros):
        linha, sentido, parada, gtfs, correspondencia, calculo, reconstrucao, estimativa, _, balde = chave
        referencia, esperado = balde
        esperado = self._recortar_apos_24h(esperado, registros)
        registros = sorted(registros, key=lambda r: r.periodo_inicio)
        janelas = [(r.periodo_inicio, r.periodo_fim) for r in registros]
        lacunas = _lacunas(esperado, janelas)
        por_data = {}
        for r in registros:
            por_data.setdefault(r.qualidade.get("data_operacao"), []).append((r.periodo_inicio, r.periodo_fim))
        sobrepostas = any(b[0] < a[1] for js in por_data.values() for a, b in zip(js, js[1:]))
        motivos = sorted({r.qualidade.get("motivo") for r in registros
                          if r.qualidade.get("motivo") and r.qualidade.get("motivo") not in INSUFICIENCIA})
        if lacunas:
            motivos.append("sem_publicacao")
        if sobrepostas:
            motivos.append("janelas_sobrepostas")
        intervalos = [s for r in registros for s in r.qualidade.get("intervalos_seg") or ()]
        if motivos:
            cv, situacao = None, "CONJUNTOS_INCOMPATIVEIS" if sobrepostas else "DADOS_AUSENTES"
        else:
            cv, motivo = coeficiente_variacao(intervalos)
            motivos = [motivo] if motivo else []
            situacao = "CALCULADA" if cv is not None else "DADOS_INSUFICIENTES"
        conjuntos = sorted({r.versao_conjunto for r in registros})
        limites = [*(x for par in esperado for x in par), *(x for par in janelas for x in par)]
        capturas = {json.dumps(r.qualidade.get("captura_programacao"), sort_keys=True) for r in registros}
        valido = not motivos or motivos[0] in INSUFICIENCIA
        return {
            "linha": linha, "sentido": sentido, "tipo": REGULARIDADE, "indicador": REGULARIDADE,
            "parada": parada, "trecho": None, "agregacao": self.agregacao, "referencia": referencia,
            "valor": publicado(cv), "unidade": UNIDADE,
            "agrupamento": agrupamento(cv), "limite_agrupamento": LIMITE_AGRUPAMENTO,
            "situacao": situacao, "motivo_ausencia": "_".join(motivos) if cv is None else None,
            "motivos": motivos,
            "intervalos": len(intervalos) if valido else None,
            "media_intervalo_seg": round(fmean(intervalos), 6) if intervalos and valido else None,
            "desvio_padrao_seg": round(pstdev(intervalos), 6) if intervalos and valido else None,
            "inicio": min(limites).isoformat(), "fim": max(limites).isoformat(),
            "duracao_horas": sum(_horas(a, b) for a, b in janelas),
            "versao_conjunto": conjuntos[0] if len(conjuntos) == 1 else _hash(conjuntos)[:32],
            "versao_gtfs_id": gtfs, "versao_correspondencia": correspondencia, "versao_calculo": calculo,
            "contagens": {"numerador": len(intervalos) if valido else None, "denominador": None},
            "qualidade": {
                "convencao": CONVENCAO, "regra_fronteira": REGRA_FRONTEIRA, "regra_agregacao": REGRA_AGREGACAO,
                "minimo_passagens": MINIMO_PASSAGENS, "oferta": OFERTA,
                "datas_operacao": sorted({r.qualidade.get("data_operacao") for r in registros}),
                "faixa": ({"hora_inicio": self.faixa[0], "hora_fim": self.faixa[1], "fuso": "America/Sao_Paulo"}
                          if self.faixa[0] else None),
                "cobertura": {"janelas": len(registros),
                              "lacunas": [{"inicio": a.isoformat(), "fim": b.isoformat()} for a, b in lacunas[:50]]},
                "versoes": {"versao_gtfs_id": gtfs, "versao_correspondencia": correspondencia,
                            "versao_reconstrucao": reconstrucao, "versao_estimativa": estimativa,
                            "versao_calculo": calculo, "versoes_conjunto": conjuntos,
                            "capturas_programacao": [json.loads(c) for c in sorted(capturas)]},
            },
        }


def consultar_regularidade(filtros):
    return PublicacaoRegularidade(filtros).consultar()
