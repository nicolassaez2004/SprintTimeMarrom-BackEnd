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
- Evidência: zero só é afirmado se a estimativa da US15-T01 rodou para as
  viagens da data (``EstimativaPassagemViagem``) e a parada não ficou sem
  evidência GPS naquela janela; sem lacuna máxima configurada, NULL.
- Sem mínimo de passagens: a regra de três passagens é da US17/US18.
"""

from datetime import datetime, time, timedelta, timezone
from hashlib import sha256
import json

from app import db
from app.indicadores.cumprimento import gravar_indicador
from app.indicadores.service import consulta_base, filtrar_periodo
from app.models import (
    EstimativaPassagemViagem, IndicadorOperacional, ImportacaoProgramacao, PartidaProgramada,
    PassagemEstimada, ViagemObservada,
)
from app.qualidade.service import FUSO, JANELA, FalhasColeta, consolidar_qualidade
from app.repositories.programacao import consultar_por_dia
from app.viagens.passagens import Percurso, lacuna_configurada, versao_estimativa

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
                 agora=None, intervalo=None, lacuna_maxima=None):
        if sentido not in ("0", "1"):
            raise ValueError("sentido deve ser '0' (ida) ou '1' (volta)")
        if not (linha and versao_gtfs_id and versao_correspondencia and versao_reconstrucao):
            raise ValueError("linha, versão GTFS, correspondência e reconstrução são obrigatórias")
        base = int(JANELA.total_seconds() // 60)
        if janela_minutos <= 0 or janela_minutos % base or 1440 % janela_minutos:
            raise ValueError(f"janela deve ser múltiplo de {base} min que divida o dia")
        self.linha, self.sentido, self.data = linha, sentido, data_operacao
        self.versao_gtfs_id = versao_gtfs_id
        lacuna_maxima = lacuna_maxima or lacuna_configurada()
        self.versoes = {"versao_correspondencia": versao_correspondencia,
                        "versao_reconstrucao": versao_reconstrucao,
                        "versao_estimativa": versao_estimativa(lacuna_maxima) if lacuna_maxima else None}
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

    def _passagens(self, tipo_oferta=None):
        """{parada: [instantes]} sem repetir a mesma evidência GPS.

        ``tipo_oferta`` restringe às viagens cujas partidas têm essa oferta
        (US17: regularidade só entre passagens da oferta por frequência)."""
        P, V, Pt, I = PassagemEstimada, ViagemObservada, PartidaProgramada, ImportacaoProgramacao
        linhas = (
            db.session.query(P.parada_codigo, P.posicao_anterior_id, P.posicao_posterior_id,
                             db.func.min(P.instante))
            .join(V, V.id == P.viagem_observada_id)
            .join(Pt, Pt.id == V.partida_programada_id)
            .join(I, I.id == Pt.importacao_id)
            .filter(P.versao_gtfs_id == self.versao_gtfs_id,
                    P.versao_estimativa == self.versoes["versao_estimativa"],
                    V.versao_reconstrucao == self.versoes["versao_reconstrucao"],
                    V.versao_correspondencia == self.versoes["versao_correspondencia"],
                    I.versao_gtfs_id == self.versao_gtfs_id, I.linha_codigo == self.linha,
                    I.sentido == self.sentido, I.data_operacao == self.data,
                    *([Pt.tipo_oferta == tipo_oferta] if tipo_oferta else []))
            .group_by(P.parada_codigo, P.posicao_anterior_id, P.posicao_posterior_id)
            .all()
        )
        por_parada = {}
        for parada, _, _, instante in linhas:
            por_parada.setdefault(parada, []).append(instante)
        return por_parada

    def _pendencias(self):
        """{parada | None: [(início, fim | None, motivo)]} em que zero não pode ser afirmado.

        ``None`` vale para todas as paradas: viagem da data sem a estimativa da
        US15-T01 executada nesta versão. Para paradas sem evidência GPS, o
        intervalo vem do registro da estimativa (limites ausentes = da viagem).
        """
        V, Pt, I, E = ViagemObservada, PartidaProgramada, ImportacaoProgramacao, EstimativaPassagemViagem
        linhas = (
            db.session.query(V.saida_observada, V.fim_observado, E.sem_evidencia, E.viagem_observada_id)
            .join(Pt, Pt.id == V.partida_programada_id)
            .join(I, I.id == Pt.importacao_id)
            .outerjoin(E, (E.viagem_observada_id == V.id)
                       & (E.versao_estimativa == self.versoes["versao_estimativa"]))
            .filter(V.versao_reconstrucao == self.versoes["versao_reconstrucao"],
                    V.versao_correspondencia == self.versoes["versao_correspondencia"],
                    I.versao_gtfs_id == self.versao_gtfs_id, I.linha_codigo == self.linha,
                    I.sentido == self.sentido, I.data_operacao == self.data)
            .all()
        )
        pendencias = {}
        for saida, fim, sem_evidencia, estimada in linhas:
            if estimada is None:
                pendencias.setdefault(None, []).append((saida, fim, "estimativa_nao_executada"))
                continue
            for item in sem_evidencia or ():
                inicio = datetime.fromisoformat(item["inicio"]) if item.get("inicio") else saida
                termino = datetime.fromisoformat(item["fim"]) if item.get("fim") else fim
                pendencias.setdefault(item["parada"], []).append((inicio, termino, "sem_evidencia_gps"))
        return pendencias

    def _janelas(self, instantes):
        meia_noite = datetime.combine(self.data, time(), FUSO).astimezone(timezone.utc)
        ultimo = max([meia_noite + timedelta(days=1) - timedelta(microseconds=1), *instantes])
        # Passagem antes da meia-noite de uma partida da data também é contada.
        antes = -((meia_noite - min([meia_noite, *instantes])) // -self.janela)
        inicio = meia_noite - antes * self.janela
        quantidade = (ultimo - inicio) // self.janela + 1
        return [(inicio + i * self.janela, inicio + (i + 1) * self.janela) for i in range(quantidade)]

    def _coleta(self, janelas):
        """Situação US13 das janelas de 15 min, pelo início em UTC."""
        primeiro_dia = janelas[0][0].astimezone(FUSO).date()
        ultimo_dia = (janelas[-1][1] - timedelta(microseconds=1)).astimezone(FUSO).date()
        relatorio = consolidar_qualidade(primeiro_dia, ultimo_dia, agora=self.agora,
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
        passagens = self._passagens() if self.versoes["versao_estimativa"] else {}
        pendencias = self._pendencias() if self.versoes["versao_estimativa"] else {}
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
                if not self.versoes["versao_estimativa"]:
                    motivo_obs = motivo_obs or "lacuna_maxima_indefinida"
                else:
                    sem_prova = sorted({m for a, b, m in [*pendencias.get(None, ()), *pendencias.get(parada, ())]
                                        if a < fim and (b is None or b >= inicio)})
                    motivo_obs = motivo_obs or ("_".join(sem_prova) if sem_prova else None)
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
        falhas = FalhasColeta(agora=parametros.get("agora"), intervalo=parametros.get("intervalo"))
        registros = [gravar_indicador(c, commit=False, falhas=falhas)
                     for c in calcular_frequencias(**parametros)]
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return registros


# --- US15-T03: publicação lado a lado (docs/US15-T03.md) ---------------------

TIPO_PUBLICO = "FREQUENCIA"
AGREGACOES = ("janela", "faixa", "dia", "mes")
REGRA_AGREGACAO = ("soma das contagens ÷ soma das durações das janelas; NULL se houver "
                   "janela nula, lacuna de publicação ou janelas sobrepostas")


def _horas(a, b):
    return (b - a).total_seconds() / 3600


def _lacunas(esperado, cobertos):
    """Partes de ``esperado`` não cobertas por nenhuma janela publicada."""
    faltas = []
    for a, b in esperado:
        for x, y in cobertos:
            if y <= a or x >= b:
                continue
            if x > a:
                faltas.append((a, x))
            a = max(a, y)
        if a < b:
            faltas.append((a, b))
    return faltas


class PublicacaoFrequencia:
    """Lê os registros da US15-T02, pareia observada/programada e agrega contagens.

    Nenhuma passagem é recontada: soma-se ``contagem_numerador`` das janelas
    publicadas e a taxa é recalculada pela soma das suas durações.
    """

    INDICADORES = (OBSERVADA, PROGRAMADA)

    def __init__(self, filtros):
        self.f = filtros
        self.agregacao = filtros.get("agregacao") or "janela"
        self.faixa = (filtros.get("hora_inicio"), filtros.get("hora_fim"))

    # --- seleção -----------------------------------------------------------------

    def _registros(self):
        I = IndicadorOperacional
        consulta = consulta_base({**self.f, "tipo": None}).filter(I.indicador.in_(self.INDICADORES))
        if self.agregacao == "janela":
            consulta = filtrar_periodo(consulta, self.f)
        else:  # +1 dia: janelas após 24h da última data operacional
            consulta = consulta.filter(I.periodo_inicio >= self.f["inicio"],
                                       I.periodo_inicio < self.f["fim"] + timedelta(days=1))
        return consulta.order_by(I.periodo_inicio, I.id).all()

    def _datas(self):
        primeira = self.f["inicio"].astimezone(FUSO).date()
        dias = (self.f["fim"].astimezone(FUSO).date() - primeira).days
        return {primeira + timedelta(days=i) for i in range(dias)}

    def _intervalo(self, data):
        """Dia operacional inteiro ou faixa local da data (UTC, fim exclusivo)."""
        a, b = (time.fromisoformat(h) if h else time() for h in self.faixa)
        fim = data + timedelta(days=1) if b <= a else data
        return (datetime.combine(data, a, FUSO).astimezone(timezone.utc),
                datetime.combine(fim, b, FUSO).astimezone(timezone.utc))

    def _balde(self, registro, datas):
        """(referência, intervalos esperados) do registro, ou None se fora da consulta."""
        data = datetime.strptime(registro.qualidade["data_operacao"], "%Y-%m-%d").date()
        if self.agregacao == "janela":  # a mesma janela civil pode ser de duas datas operacionais
            return data.isoformat(), ((registro.periodo_inicio, registro.periodo_fim),)
        if data not in datas:
            return False
        a, b = self._intervalo(data)
        if self.faixa[0] and not (a <= registro.periodo_inicio and registro.periodo_fim <= b):
            return False
        if self.agregacao != "mes":
            return data.isoformat(), ((a, b),)
        mes = [d for d in sorted(datas) if (d.year, d.month) == (data.year, data.month)]
        return data.strftime("%Y-%m"), tuple(self._intervalo(d) for d in mes)

    # --- agregação ---------------------------------------------------------------

    def consultar(self):
        datas = self._datas() if self.agregacao != "janela" else None
        grupos = {}
        for r in self._registros():
            balde = self._balde(r, datas)
            if balde is False:
                continue
            q = r.qualidade or {}
            # Só soma o que é comparável: mesma linha/sentido/parada, versões e tamanho de janela.
            chave = (r.linha_codigo, r.sentido, r.parada_codigo, r.versao_gtfs_id, r.versao_correspondencia,
                     r.versao_calculo, q.get("versao_reconstrucao"), q.get("versao_estimativa"),
                     r.periodo_fim - r.periodo_inicio, balde)
            grupos.setdefault(chave, []).append(r)
        itens = sorted((self._com_falhas_coleta(self._item(chave, rs), rs) for chave, rs in grupos.items()),
                       key=lambda x: (x["inicio"], x["sentido"] or "", x["parada"] or "", x["versao_conjunto"]))
        pagina, tamanho = self.f["page"], self.f["page_size"]
        return {"dados": itens[(pagina - 1) * tamanho:pagina * tamanho],
                "paginacao": {"page": pagina, "page_size": tamanho, "total": len(itens)}}

    def _com_falhas_coleta(self, item, registros):
        """US26: na agregação ``janela`` a janela exibida é a publicada; repassa o bloco
        gravado (o mesmo para todos os registros da janela). Nas demais agregações a
        janela exibida é outra e o bloco não se aplica."""
        bloco = next(((r.qualidade or {}).get("falhas_coleta") for r in registros
                      if "falhas_coleta" in (r.qualidade or {})), None)
        if self.agregacao == "janela" and bloco is not None:
            item["qualidade"]["falhas_coleta"] = bloco
        return item

    @staticmethod
    def _lado(registros, esperado, zero, ausente):
        registros = sorted(registros, key=lambda r: r.periodo_inicio)
        janelas = [(r.periodo_inicio, r.periodo_fim) for r in registros]
        lacunas = _lacunas(esperado, janelas)
        validas = [r for r in registros if r.valor is not None]
        publicada = sum(_horas(a, b) for a, b in janelas)
        parcial = sum(r.contagem_numerador for r in validas)
        motivos = sorted({(r.qualidade or {}).get("motivo") or "valor_nulo"
                          for r in registros if r.valor is None})
        if not registros:
            motivos = [ausente]
        elif lacunas:
            motivos.append("sem_publicacao")
        # Janelas após 24h de uma data cobrem o mesmo horário civil da data seguinte;
        # sobreposição só é conflito dentro da mesma data operacional.
        por_data = {}
        for r in registros:
            por_data.setdefault((r.qualidade or {}).get("data_operacao"), []).append(
                (r.periodo_inicio, r.periodo_fim))
        sobrepostas = any(b[0] < a[1] for js in por_data.values() for a, b in zip(js, js[1:]))
        if sobrepostas:
            motivos.append("janelas_sobrepostas")
        if motivos:
            situacao = ("CONJUNTOS_INCOMPATIVEIS" if sobrepostas else
                        "COBERTURA_PARCIAL" if validas else "DADOS_AUSENTES")
            valor = contagem = None
        else:
            contagem = parcial
            valor = round(contagem / publicada, 6)
            situacao = "CALCULADA" if contagem else zero
        lacuna_h = sum(_horas(a, b) for a, b in lacunas)
        valida = sum(_horas(r.periodo_inicio, r.periodo_fim) for r in validas)
        return {
            "valor": valor, "unidade": UNIDADE, "contagem": contagem, "situacao": situacao,
            "motivos": motivos,
            "cobertura": {
                "janelas": len(registros), "janelas_validas": len(validas),
                "duracao_esperada_horas": sum(_horas(a, b) for a, b in esperado),
                "duracao_publicada_horas": publicada, "duracao_valida_horas": valida,
                "fracao_valida": round(valida / (publicada + lacuna_h), 6) if publicada + lacuna_h else None,
                "contagem_valida_parcial": parcial if motivos and registros else None,
                "lacunas": [{"inicio": a.isoformat(), "fim": b.isoformat()} for a, b in lacunas[:50]],
            },
        }

    def _recortar_apos_24h(self, esperado, registros):
        """Faixa após a meia-noite: a US15-T02 só publica janelas da data D depois
        de 24h quando há partida ou passagem de D ali; sem elas, a operação de D
        terminou à meia-noite e esse trecho não é lacuna de D."""
        if not self.faixa[0]:
            return esperado
        recortado = []
        for a, b in esperado:
            data = a.astimezone(FUSO).date()
            meia_noite = datetime.combine(data + timedelta(days=1), time(), FUSO).astimezone(timezone.utc)
            if b > meia_noite:
                publicado = max((r.periodo_fim for r in registros
                                 if (r.qualidade or {}).get("data_operacao") == data.isoformat()),
                                default=meia_noite)
                b = min(b, max(meia_noite, publicado))
            recortado.append((a, b))
        return tuple(recortado)

    def _item(self, chave, registros):
        linha, sentido, parada, gtfs, correspondencia, calculo, reconstrucao, estimativa, _, balde = chave
        referencia, esperado = balde
        esperado = self._recortar_apos_24h(esperado, registros)
        obs = [r for r in registros if r.indicador == OBSERVADA]
        prog = [r for r in registros if r.indicador == PROGRAMADA]
        observada = self._lado(obs, esperado, "ZERO_OBSERVADO", "observada_nao_publicada")
        programada = self._lado(prog, esperado, "ZERO_PROGRAMADO", "programada_nao_publicada_na_parada")
        limites = [*(x for par in esperado for x in par),
                   *(x for r in registros for x in (r.periodo_inicio, r.periodo_fim))]
        conjuntos = sorted({r.versao_conjunto for r in registros})
        capturas = {json.dumps(r.qualidade.get("captura_programacao"), sort_keys=True) for r in prog}
        qualidade = {
            "motivos_ausencia": sorted({*observada["motivos"], *programada["motivos"]}),
            "regra_agregacao": REGRA_AGREGACAO,
            "datas_operacao": sorted({r.qualidade.get("data_operacao") for r in registros}),
            "faixa": ({"hora_inicio": self.faixa[0], "hora_fim": self.faixa[1], "fuso": str(FUSO)}
                      if self.faixa[0] else None),
            "versoes": {
                "versao_gtfs_id": gtfs, "versao_correspondencia": correspondencia,
                "versao_reconstrucao": reconstrucao, "versao_estimativa": estimativa,
                "versao_calculo": calculo, "versoes_conjunto": conjuntos,
                "capturas_programacao": [json.loads(c) for c in sorted(capturas)],
            },
            "minimo_passagens": "não aplicado (US17/US18)",
        }
        if self.agregacao == "janela" and obs:
            qualidade["coleta"] = obs[0].qualidade.get("coleta")
        return {
            "linha": linha, "sentido": sentido, "tipo": TIPO_PUBLICO, "indicador": TIPO_PUBLICO,
            "parada": parada, "trecho": None, "agregacao": self.agregacao, "referencia": referencia,
            "valor": observada["valor"], "unidade": UNIDADE,
            "inicio": min(limites).isoformat(), "fim": max(limites).isoformat(),
            "duracao_horas": observada["cobertura"]["duracao_publicada_horas"] or
                             programada["cobertura"]["duracao_publicada_horas"],
            "versao_conjunto": conjuntos[0] if len(conjuntos) == 1 else _hash(conjuntos)[:32],
            "versao_gtfs_id": gtfs, "versao_correspondencia": correspondencia, "versao_calculo": calculo,
            "contagens": {"numerador": observada["contagem"], "denominador": None},
            "observada": observada, "programada": programada, "qualidade": qualidade,
        }


def consultar_frequencias(filtros):
    return PublicacaoFrequencia(filtros).consultar()
