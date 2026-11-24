"""US15-T02: passagens observadas e programadas por linha, sentido, parada e janela.

Regras (docs/US15-T02.md):

- Janelas [início, fim) de 15 min (ou múltiplos) a partir da meia-noite de São
  Paulo da data operacional, estendidas até cobrir partidas/passagens após 24h.
- Frequência = passagens ÷ duração da janela em horas (``passagens/h``).
- Observada: ``PassagemEstimada`` da US15-T01 das viagens cuja partida é da
  data operacional; a mesma evidência GPS (parada + par de posições) conta uma vez.
- Programada: partidas da captura escolhida pela US04 (``consultar_por_dia``).
  São saídas da primeira parada; só a parada de origem do percurso recebe
  frequência programada. Paradas intermediárias não têm horário previsto.
- Confiabilidade: situação das janelas de 15 min da US13. COMPLETA/COM_FALHAS
  permitem afirmar zero; as demais tornam o valor NULL, com motivo.
- Sem mínimo de passagens: a regra de três passagens é da US17/US18.
"""

from datetime import datetime, time, timedelta, timezone
from hashlib import sha256
import json

from app import db
from app.indicadores.cumprimento import gravar_indicador
from app.models import ImportacaoProgramacao, PartidaProgramada, PassagemEstimada, ViagemObservada
from app.qualidade.service import FUSO, JANELA, consolidar_qualidade
from app.repositories.programacao import consultar_por_dia
from app.viagens.passagens import VERSAO as VERSAO_ESTIMATIVA
from app.viagens.passagens import Percurso

VERSAO_CALCULO = "frequencia-1.0"
UNIDADE = "passagens/h"
OBSERVADA, PROGRAMADA = "FREQUENCIA_OBSERVADA", "FREQUENCIA_PROGRAMADA"
COLETA_CONFIAVEL = frozenset({"COMPLETA", "COM_FALHAS"})


def _hash(dados):
    return sha256(json.dumps(dados, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class CalculoFrequencia:
    """Uma linha/sentido/data operacional, com as versões que identificam o conjunto."""

    def __init__(self, *, linha, sentido, data_operacao, versao_gtfs_id,
                 versao_correspondencia, versao_reconstrucao, janela_minutos=15,
                 agora=None, intervalo=None):
        if sentido not in ("0", "1"):
            raise ValueError("sentido deve ser '0' (ida) ou '1' (volta)")
        if not (linha and versao_gtfs_id and versao_correspondencia and versao_reconstrucao):
            raise ValueError("linha, versão GTFS, correspondência e reconstrução são obrigatórias")
        base = int(JANELA.total_seconds() // 60)
        if janela_minutos <= 0 or janela_minutos % base or 1440 % janela_minutos:
            raise ValueError(f"janela deve ser múltiplo de {base} min que divida o dia")
        self.linha, self.sentido, self.data = linha, sentido, data_operacao
        self.versao_gtfs_id = versao_gtfs_id
        self.versoes = {"versao_correspondencia": versao_correspondencia,
                        "versao_reconstrucao": versao_reconstrucao,
                        "versao_estimativa": VERSAO_ESTIMATIVA}
        self.janela = timedelta(minutes=janela_minutos)
        self.agora, self.intervalo = agora, intervalo

    # --- fontes reutilizadas -------------------------------------------------

    def _programacao(self):
        importacao, tipos = consultar_por_dia(self.versao_gtfs_id, self.linha, self.sentido, self.data)
        if importacao is None:
            return None, [], "programacao_ambigua" if tipos else "programacao_ausente"
        captura = {"importacao_id": importacao.id, "fonte": importacao.fonte,
                   "versao_fonte": importacao.versao_fonte, "tipo_dia": importacao.tipo_dia}
        return captura, [p.saida_prevista for p in importacao.partidas], None

    def _passagens(self):
        """{parada: [instantes]} sem repetir a mesma evidência GPS."""
        P, V, Pt, I = PassagemEstimada, ViagemObservada, PartidaProgramada, ImportacaoProgramacao
        linhas = (
            db.session.query(P.parada_codigo, P.posicao_anterior_id, P.posicao_posterior_id,
                             db.func.min(P.instante))
            .join(V, V.id == P.viagem_observada_id)
            .join(Pt, Pt.id == V.partida_programada_id)
            .join(I, I.id == Pt.importacao_id)
            .filter(P.versao_gtfs_id == self.versao_gtfs_id,
                    P.versao_estimativa == VERSAO_ESTIMATIVA,
                    V.versao_reconstrucao == self.versoes["versao_reconstrucao"],
                    V.versao_correspondencia == self.versoes["versao_correspondencia"],
                    I.versao_gtfs_id == self.versao_gtfs_id, I.linha_codigo == self.linha,
                    I.sentido == self.sentido, I.data_operacao == self.data)
            .group_by(P.parada_codigo, P.posicao_anterior_id, P.posicao_posterior_id)
            .all()
        )
        por_parada = {}
        for parada, _, _, instante in linhas:
            por_parada.setdefault(parada, []).append(instante)
        return por_parada

    def _janelas(self, instantes):
        inicio = datetime.combine(self.data, time(), FUSO).astimezone(timezone.utc)
        ultimo = max([inicio + timedelta(days=1) - timedelta(microseconds=1), *instantes])
        quantidade = (ultimo - inicio) // self.janela + 1
        return [(inicio + i * self.janela, inicio + (i + 1) * self.janela) for i in range(quantidade)]

    def _coleta(self, janelas):
        """Situação US13 das janelas de 15 min, pelo início em UTC."""
        ultimo_dia = (janelas[-1][1] - timedelta(microseconds=1)).astimezone(FUSO).date()
        relatorio = consolidar_qualidade(self.data, ultimo_dia, agora=self.agora,
                                         intervalo=self.intervalo, incluir_janelas=True)
        return {j["inicio"]: j for dia in relatorio["dias"] for j in dia["janelas"]}

    # --- cálculo ------------------------------------------------------------------

    def calcular(self):
        """Campos de ``IndicadorOperacional`` de cada janela/parada, sem gravar."""
        percurso, motivo = Percurso.da_rede(self.versao_gtfs_id, self.linha, self.sentido)
        if percurso is None:
            raise ValueError(f"paradas da linha/sentido indisponíveis: {motivo}")
        paradas = list(dict.fromkeys(codigo for codigo, _ in percurso.paradas))
        captura, partidas, motivo_programacao = self._programacao()
        passagens = self._passagens()
        janelas = self._janelas(partidas + [t for ts in passagens.values() for t in ts])
        coleta = self._coleta(janelas)
        contexto = {"linha": self.linha, "sentido": self.sentido, "data_operacao": self.data.isoformat(),
                    "versao_gtfs_id": self.versao_gtfs_id, "versao_calculo": VERSAO_CALCULO,
                    "janela_minutos": int(self.janela.total_seconds() // 60), **self.versoes,
                    "captura_programacao": captura}
        self.versao_conjunto = _hash(contexto)[:32]

        campos = []
        for inicio, fim in janelas:
            sub = [coleta.get((inicio + k * JANELA).isoformat())
                   for k in range(self.janela // JANELA)]
            situacoes = [j["situacao"] if j else "FORA_DO_RELATORIO" for j in sub]
            ruins = sorted({s for s in situacoes if s not in COLETA_CONFIAVEL})
            qualidade_coleta = {
                "situacoes_15min": situacoes,
                "percentual_falhas": [j and j["percentual_falhas"] for j in sub],
                "confiavel": not ruins,
            }
            for parada in paradas:
                n = sum(inicio <= t < fim for t in passagens.get(parada, ()))
                motivo_obs = "coleta_" + "_".join(ruins).lower() if ruins else None
                campos.append(self._campos(OBSERVADA, parada, inicio, fim, n, motivo_obs, {
                    "coleta": qualidade_coleta, "passagens_identificadas": n,
                    "dedupe": "parada + posições GPS anterior/posterior"}))
            if paradas:
                n = sum(inicio <= t < fim for t in partidas)
                campos.append(self._campos(PROGRAMADA, paradas[0], inicio, fim,
                                           None if motivo_programacao else n, motivo_programacao, {
                    "captura_programacao": captura, "parada_origem": True,
                    "base": "saida_prevista da primeira parada (US04)"}))
        return campos

    def _campos(self, indicador, parada, inicio, fim, n, motivo, qualidade):
        horas = (fim - inicio).total_seconds() / 3600
        if motivo:
            situacao, valor, numerador = "DADOS_AUSENTES", None, None
        else:
            situacao = "CALCULADA" if n else ("ZERO_OBSERVADO" if indicador == OBSERVADA else "ZERO_PROGRAMADO")
            valor, numerador = round(n / horas, 6), n
        chave = _hash({"indicador": indicador, "parada": parada, "inicio": inicio.isoformat(),
                       "fim": fim.isoformat(), "conjunto": self.versao_conjunto})
        return {
            "chave_resultado": chave, "versao_gtfs_id": self.versao_gtfs_id,
            "linha_codigo": self.linha, "sentido": self.sentido, "parada_codigo": parada,
            "trecho_id": None, "indicador": indicador, "valor": valor, "unidade": UNIDADE,
            "periodo_inicio": inicio, "periodo_fim": fim,
            "contagem_numerador": numerador, "contagem_denominador": None,
            "versao_conjunto": self.versao_conjunto,
            "versao_correspondencia": self.versoes["versao_correspondencia"],
            "versao_calculo": VERSAO_CALCULO,
            "qualidade": {
                "situacao": situacao, "motivo": motivo, "duracao_horas": horas,
                "data_operacao": self.data.isoformat(), **self.versoes,
                "minimo_passagens": "não aplicado (US17/US18)", **qualidade,
            },
        }


def calcular_frequencias(**parametros):
    return CalculoFrequencia(**parametros).calcular()


def calcular_e_publicar_frequencias(**parametros):
    """Calcula e grava o conjunto numa única transação (upsert por chave)."""
    try:
        registros = [gravar_indicador(c, commit=False) for c in calcular_frequencias(**parametros)]
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return registros
