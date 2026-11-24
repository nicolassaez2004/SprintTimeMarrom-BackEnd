"""US18: tempo de espera estimado e excesso de espera por parada.

Regras (docs/US18.md):

- T01, espera estimada: ``E = Σh² ÷ (2·Σh)`` sobre os intervalos ``h`` entre
  passagens sucessivas da mesma linha/sentido/parada, os mesmos da US17-T01
  (oferta por frequência, duplicação tratada, intervalo na janela da passagem
  posterior). Intervalos em segundos; o valor é publicado em **minutos**
  (``E_s ÷ 60``). NULL com menos de três passagens (dois intervalos) ou divisor
  inválido (``Σh = 0``).
- T02, excesso de espera: ``excesso = E − E_prog``. Com um único intervalo
  programado ``H`` vigente na janela, ``E_prog = H ÷ 2`` (o cartão). Quando a
  janela cruza faixas de programação, a mesma fórmula da espera é aplicada aos
  intervalos programados vigentes: ``E_prog = ΣH² ÷ (2·ΣH)``, que vale ``H ÷ 2``
  quando todos são iguais. Vigente na janela = partida da oferta por frequência
  da captura escolhida pela US04 cujo intervalo ``[saída, saída + H)`` cruza a
  janela. Excesso negativo é mantido, sem truncar. Sem intervalo programado:
  NULL com ``intervalo_programado_ausente``.
- Confiabilidade e motivos de ausência: os mesmos da US17 (coleta, evidência
  GPS, estimativa, oferta, programação).
"""

from datetime import timedelta
import json

from app import db
from app.indicadores.cumprimento import gravar_indicador
from app.indicadores.frequencia import _hash, _horas, _lacunas
from app.indicadores.regularidade import (
    INSUFICIENCIA, MINIMO_PASSAGENS, OFERTA, REGRA_FRONTEIRA, CalculoRegularidade, PublicacaoRegularidade,
)
from app.repositories.programacao import consultar_por_dia

TIPO_PUBLICO = "ESPERA"
ESPERA, EXCESSO = "ESPERA_ESTIMADA", "EXCESSO_ESPERA"
VERSAO_CALCULO = "espera-1.0"
UNIDADE = "min"
FORMULA_ESPERA = "E = Σh² ÷ (2·Σh) dos intervalos h entre passagens sucessivas (s), publicada em minutos"
REGRA_EXCESSO = ("excesso = E − E_prog; E_prog = ΣH² ÷ (2·ΣH) dos intervalos programados vigentes na "
                 "janela (= H ÷ 2 com intervalo único); excesso negativo mantido, sem truncar")
REGRA_VIGENCIA = ("partida da oferta por frequência da captura da US04 cujo intervalo [saída, saída + H) "
                  "cruza a janela")
INSUFICIENCIA_ESPERA = INSUFICIENCIA | {"divisor_invalido"}


def espera_estimada(intervalos_seg):
    """``(espera_s, motivo)``: Σh² ÷ 2Σh; NULL com menos de 3 passagens ou Σh inválida."""
    if len(intervalos_seg) < MINIMO_PASSAGENS - 1:
        return None, "passagens_insuficientes"
    soma = sum(intervalos_seg)
    if not soma > 0:
        return None, "divisor_invalido"
    return sum(h * h for h in intervalos_seg) / (2 * soma), None


def espera_programada(intervalos_programados_seg):
    """``ΣH² ÷ 2ΣH``; ``H ÷ 2`` com intervalo único. None sem intervalo programado."""
    soma = sum(intervalos_programados_seg)
    if not intervalos_programados_seg or not soma > 0:
        return None
    return sum(h * h for h in intervalos_programados_seg) / (2 * soma)


def _minutos(segundos):
    return None if segundos is None else round(segundos / 60, 6)


class CalculoEspera(CalculoRegularidade):
    """Reaproveita janelas, intervalos e bloqueios da US17 (nenhum algoritmo repetido)."""

    def _vigentes(self):
        importacao, _ = consultar_por_dia(self.versao_gtfs_id, self.linha, self.sentido, self.data)
        return [] if importacao is None else sorted(
            (p.saida_prevista, p.intervalo_seg, p.id) for p in importacao.partidas
            if p.tipo_oferta == OFERTA and p.intervalo_seg)

    def calcular(self):
        vigentes = self._vigentes()
        campos = []
        for inicio, fim, parada, da_janela, bloqueio in self.janelas_com_intervalos(ESPERA, VERSAO_CALCULO):
            intervalos = [s for _, _, s in da_janela]
            programadas = [(i, h) for t, h, i in vigentes
                           if t < fim and t + timedelta(seconds=h) > inicio]
            campos.extend(self._campos(parada, inicio, fim, intervalos, bloqueio, programadas))
        return campos

    def _campos(self, parada, inicio, fim, intervalos, bloqueio, programadas):
        if bloqueio:
            espera, motivo = None, "_".join(bloqueio)
        else:
            espera, motivo = espera_estimada(intervalos)
        prog = espera_programada([h for _, h in programadas])
        if espera is None:
            excesso, motivo_excesso = None, motivo
        elif prog is None:
            excesso, motivo_excesso = None, "intervalo_programado_ausente"
        else:
            excesso, motivo_excesso = espera - prog, None
        comum = {
            "versao_gtfs_id": self.versao_gtfs_id, "linha_codigo": self.linha, "sentido": self.sentido,
            "parada_codigo": parada, "trecho_id": None, "unidade": UNIDADE,
            "periodo_inicio": inicio, "periodo_fim": fim, "contagem_denominador": None,
            "versao_conjunto": self.versao_conjunto,
            "versao_correspondencia": self.versoes["versao_correspondencia"], "versao_calculo": VERSAO_CALCULO,
        }
        qualidade = {
            "intervalos_seg": [] if bloqueio else intervalos,
            "intervalos_programados": [{"partida_id": i, "intervalo_seg": h} for i, h in programadas],
            "espera_programada_min": _minutos(prog), "minimo_passagens": MINIMO_PASSAGENS,
            "formula_espera": FORMULA_ESPERA, "regra_excesso": REGRA_EXCESSO, "regra_vigencia": REGRA_VIGENCIA,
            "regra_fronteira": REGRA_FRONTEIRA, "oferta": OFERTA, "duracao_horas": _horas(inicio, fim),
            "data_operacao": self.data.isoformat(), "captura_programacao": self.captura, **self.versoes,
        }
        campos = []
        for indicador, valor, motivo_lado in ((ESPERA, espera, motivo), (EXCESSO, excesso, motivo_excesso)):
            situacao = ("CALCULADA" if valor is not None else
                        "DADOS_INSUFICIENTES" if motivo_lado in INSUFICIENCIA_ESPERA else "DADOS_AUSENTES")
            chave = _hash({"indicador": indicador, "parada": parada, "inicio": inicio.isoformat(),
                           "fim": fim.isoformat(), "conjunto": self.versao_conjunto})
            campos.append({**comum, "chave_resultado": chave, "indicador": indicador,
                           "valor": _minutos(valor),
                           "contagem_numerador": None if bloqueio else len(intervalos),
                           "qualidade": {**qualidade, "situacao": situacao, "motivo": motivo_lado}})
        return campos


def calcular_espera(**parametros):
    return CalculoEspera(**parametros).calcular()


def calcular_e_publicar_espera(**parametros):
    """Espera e excesso de cada janela gravados juntos, numa transação."""
    try:
        registros = [gravar_indicador(c, commit=False) for c in calcular_espera(**parametros)]
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return registros


# --- US18-T03A: consulta com os dois valores separados ------------------------------

class PublicacaoEspera(PublicacaoRegularidade):
    """Agrega pela união dos intervalos observados e programados de cada balde."""

    INDICADORES = (ESPERA, EXCESSO)

    def _lado(self, registros, esperado, agregar):
        registros = sorted(registros, key=lambda r: r.periodo_inicio)
        janelas = [(r.periodo_inicio, r.periodo_fim) for r in registros]
        lacunas = _lacunas(esperado, janelas)
        por_data = {}
        for r in registros:
            por_data.setdefault(r.qualidade.get("data_operacao"), []).append((r.periodo_inicio, r.periodo_fim))
        sobrepostas = any(b[0] < a[1] for js in por_data.values() for a, b in zip(js, js[1:]))
        motivos = sorted({r.qualidade.get("motivo") for r in registros
                          if r.qualidade.get("motivo") and r.qualidade["motivo"] not in INSUFICIENCIA_ESPERA
                          and r.qualidade["motivo"] != "intervalo_programado_ausente"})
        if not registros:
            motivos.append("nao_publicado")
        if lacunas:
            motivos.append("sem_publicacao")
        if sobrepostas:
            motivos.append("janelas_sobrepostas")
        if self.agregacao == "janela" and registros:  # a própria janela publicada, sem recalcular
            r = registros[0]
            valor, motivo = r.valor, r.qualidade.get("motivo")
            motivos = [motivo] if motivo else []
        elif motivos:
            valor = None
        else:
            valor, motivo = agregar(registros)
            motivos = [motivo] if motivo else []
        situacao = ("CALCULADA" if valor is not None else
                    "CONJUNTOS_INCOMPATIVEIS" if "janelas_sobrepostas" in motivos else
                    "DADOS_INSUFICIENTES" if set(motivos) <= INSUFICIENCIA_ESPERA else "DADOS_AUSENTES")
        return {"valor": valor, "unidade": UNIDADE, "situacao": situacao,
                "motivo_ausencia": "_".join(motivos) if valor is None else None,
                "lacunas": [{"inicio": a.isoformat(), "fim": b.isoformat()} for a, b in lacunas[:50]]}, janelas

    @staticmethod
    def _uniao(registros):
        intervalos = [s for r in registros for s in r.qualidade.get("intervalos_seg") or ()]
        programadas = {}
        for r in registros:
            for p in r.qualidade.get("intervalos_programados") or ():
                programadas[p["partida_id"]] = p["intervalo_seg"]  # partida vigente em duas janelas conta uma vez
        return intervalos, list(programadas.values())

    def _item(self, chave, registros):
        linha, sentido, parada, gtfs, correspondencia, calculo, reconstrucao, estimativa, _, balde = chave
        referencia, esperado = balde
        esperado = self._recortar_apos_24h(esperado, registros)
        de_espera = [r for r in registros if r.indicador == ESPERA]
        de_excesso = [r for r in registros if r.indicador == EXCESSO]

        def agregar_espera(rs):
            espera, motivo = espera_estimada(self._uniao(rs)[0])
            return _minutos(espera), motivo

        def agregar_excesso(rs):
            intervalos, programados = self._uniao(rs)
            espera, motivo = espera_estimada(intervalos)
            if espera is None:
                return None, motivo
            prog = espera_programada(programados)
            return (None, "intervalo_programado_ausente") if prog is None else (_minutos(espera - prog), None)

        lado_espera, janelas = self._lado(de_espera, esperado, agregar_espera)
        lado_excesso, _ = self._lado(de_excesso, esperado, agregar_excesso)
        intervalos, programados = self._uniao(de_espera)
        conjuntos = sorted({r.versao_conjunto for r in registros})
        limites = [*(x for par in esperado for x in par), *(x for par in janelas for x in par)]
        capturas = {json.dumps(r.qualidade.get("captura_programacao"), sort_keys=True) for r in registros}
        lado_excesso["espera_programada_min"] = _minutos(espera_programada(programados))
        lado_excesso["intervalos_programados_seg"] = programados
        return {
            "linha": linha, "sentido": sentido, "tipo": TIPO_PUBLICO, "indicador": TIPO_PUBLICO,
            "parada": parada, "trecho": None, "agregacao": self.agregacao, "referencia": referencia,
            "inicio": min(limites).isoformat(), "fim": max(limites).isoformat(),
            "duracao_horas": sum(_horas(a, b) for a, b in janelas), "unidade": UNIDADE,
            "espera_estimada": lado_espera, "excesso_espera": lado_excesso,
            "intervalos": len(intervalos) if lado_espera["valor"] is not None else None,
            "valor": lado_espera["valor"],
            "versao_conjunto": conjuntos[0] if len(conjuntos) == 1 else _hash(conjuntos)[:32],
            "versao_gtfs_id": gtfs, "versao_correspondencia": correspondencia, "versao_calculo": calculo,
            "contagens": {"numerador": len(intervalos) if lado_espera["valor"] is not None else None,
                          "denominador": None},
            "qualidade": {
                "formula_espera": FORMULA_ESPERA, "regra_excesso": REGRA_EXCESSO,
                "regra_vigencia": REGRA_VIGENCIA, "regra_fronteira": REGRA_FRONTEIRA,
                "minimo_passagens": MINIMO_PASSAGENS, "oferta": OFERTA,
                "datas_operacao": sorted({r.qualidade.get("data_operacao") for r in registros}),
                "faixa": ({"hora_inicio": self.faixa[0], "hora_fim": self.faixa[1], "fuso": "America/Sao_Paulo"}
                          if self.faixa[0] else None),
                "versoes": {"versao_gtfs_id": gtfs, "versao_correspondencia": correspondencia,
                            "versao_reconstrucao": reconstrucao, "versao_estimativa": estimativa,
                            "versao_calculo": calculo, "versoes_conjunto": conjuntos,
                            "capturas_programacao": [json.loads(c) for c in sorted(capturas)]},
            },
        }


def consultar_espera(filtros):
    return PublicacaoEspera(filtros).consultar()
