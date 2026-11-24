"""US12-T04: comprova que um mês de indicadores publicados é reproduzível.

Executar: ``python -m app.indicadores.reproducao --mes AAAA-MM``.

Não grava em ``registros_gps`` nem em ``indicadores_operacionais``: recalcula
com as funções puras das US14/US16 (``calcular_indicador_*``) e compara. Com
``--reprocessar-brutos`` reutiliza o manifesto da US12-T03, que grava apenas
em ``execucoes_processamento``/``resultados_reprocessamento`` (não usar na
base compartilhada).
"""

import argparse
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
from zoneinfo import ZoneInfo

from sqlalchemy import func, inspect, text

from app import db
from app.indicadores.cumprimento import VERSAO_ALGORITMO, calcular_indicador_cumprimento
from app.indicadores.pontualidade import (
    VERSAO_ALGORITMO_PONTUALIDADE, calcular_indicador_pontualidade,
)
from app.models import IndicadorOperacional, RegistroBrutoGPS, ResultadoReprocessamento
from app.persistencia.reprocessamento import (
    criar_execucao_reprocessamento, executar_reprocessamento,
)

FUSO = ZoneInfo("America/Sao_Paulo")
CALCULOS = {
    "CUMPRIMENTO_VIAGENS": (calcular_indicador_cumprimento, VERSAO_ALGORITMO),
    "PONTUALIDADE_VIAGENS": (calcular_indicador_pontualidade, VERSAO_ALGORITMO_PONTUALIDADE),
}
VERSAO_REGRA = "+".join(versao for _, versao in CALCULOS.values())
CAMPOS = (
    "chave_resultado", "versao_conjunto", "indicador", "unidade", "linha_codigo",
    "sentido", "parada_codigo", "trecho_id", "periodo_inicio", "periodo_fim",
    "versao_gtfs_id", "versao_correspondencia", "versao_calculo",
    "contagem_numerador", "contagem_denominador", "valor", "qualidade",
)
# Hoje nenhuma rotina persiste ViagemObservada a partir dos brutos (US14-T01C);
# o recálculo parte das viagens reconstruídas. Mudar quando a cadeia existir.
RECONSTRUCAO_A_PARTIR_DOS_BRUTOS = False


def _json(valor):
    return json.dumps(valor, sort_keys=True, separators=(",", ":"), default=str)


def _iso(valor):
    return valor.isoformat() if isinstance(valor, datetime) else valor


class ReproducaoIndicadores:
    """Seleciona um mês, fixa versões, recalcula e compara sem alterar o histórico."""

    def __init__(self, mes, *, versao_base, versao_codigo, versao_regra,
                 tolerancia_valor=None, reprocessar_brutos=False,
                 dados_sinteticos=False, agora=None):
        ano, numero = (int(parte) for parte in mes.split("-"))
        self.mes = f"{ano:04d}-{numero:02d}"
        proximo = date(ano + numero // 12, numero % 12 + 1, 1)
        self.dias = [date(ano, numero, 1) + timedelta(d)
                     for d in range((proximo - date(ano, numero, 1)).days)]
        self.inicio = datetime(ano, numero, 1, tzinfo=FUSO).astimezone(timezone.utc)
        self.fim = datetime(proximo.year, proximo.month, 1, tzinfo=FUSO).astimezone(timezone.utc)
        if tolerancia_valor is not None and tolerancia_valor < 0:
            raise ValueError("tolerância deve ser não negativa")
        self.versoes = {"base": versao_base, "codigo": versao_codigo, "regra": versao_regra}
        if not all(isinstance(v, str) and v.strip() for v in self.versoes.values()):
            raise ValueError("versões de base, código e regra são obrigatórias")
        if versao_regra != VERSAO_REGRA:
            raise ValueError(f"versão de regra {versao_regra} difere do código ({VERSAO_REGRA})")
        self.tolerancia = tolerancia_valor
        self.reprocessar_brutos = reprocessar_brutos
        self.dados_sinteticos = dados_sinteticos
        self.agora = agora or datetime.now(timezone.utc)

    # -- seleção e preservação -------------------------------------------------
    def _brutos(self):
        return RegistroBrutoGPS.query.filter(
            RegistroBrutoGPS.coletado_em >= self.inicio,
            RegistroBrutoGPS.coletado_em < self.fim,
        )

    def _publicados(self):
        return (IndicadorOperacional.query
                .filter(IndicadorOperacional.periodo_inicio >= self.inicio,
                        IndicadorOperacional.periodo_fim <= self.fim)
                .order_by(IndicadorOperacional.periodo_inicio, IndicadorOperacional.linha_codigo,
                          IndicadorOperacional.sentido, IndicadorOperacional.indicador,
                          IndicadorOperacional.id))

    def _impressao(self):
        """Hash dos brutos e dos publicados do mês, para provar que nada mudou."""
        brutos = sha256()
        quantidade = 0
        colunas = (RegistroBrutoGPS.id, RegistroBrutoGPS.payload, RegistroBrutoGPS.latitude,
                   RegistroBrutoGPS.longitude, RegistroBrutoGPS.horario_gps,
                   RegistroBrutoGPS.coletado_em, RegistroBrutoGPS.prefixo_origem)
        for linha in self._brutos().with_entities(*colunas).order_by(RegistroBrutoGPS.id).yield_per(5000):
            brutos.update(_json(list(linha)).encode())
            quantidade += 1
        publicados = sha256()
        for registro in self._publicados():
            publicados.update(_json([registro.id, str(registro.publicado_em)]
                                    + [getattr(registro, c) for c in CAMPOS]).encode())
        return {"brutos": quantidade, "brutos_sha256": brutos.hexdigest(),
                "publicados_sha256": publicados.hexdigest()}

    def _cruzam_virada(self):
        """Janelas publicadas que começam ou terminam fora do mês."""
        return IndicadorOperacional.query.filter(
            IndicadorOperacional.periodo_inicio < self.fim,
            IndicadorOperacional.periodo_fim > self.inicio,
            (IndicadorOperacional.periodo_inicio < self.inicio)
            | (IndicadorOperacional.periodo_fim > self.fim),
        ).count()

    def _completude(self, publicados):
        def meia_noite(dia):
            return datetime(dia.year, dia.month, dia.day, tzinfo=FUSO).astimezone(timezone.utc)

        dias_sem_brutos = [
            dia.isoformat() for dia in self.dias
            if not db.session.query(self._brutos().filter(
                RegistroBrutoGPS.coletado_em >= meia_noite(dia),
                RegistroBrutoGPS.coletado_em < meia_noite(dia + timedelta(1)),
            ).exists()).scalar()
        ]
        fechado = self.fim <= self.agora
        motivos = ([] if fechado else ["MES_NAO_ENCERRADO"]) + \
            ([f"{len(dias_sem_brutos)} dia(s) sem brutos"] if dias_sem_brutos else []) + \
            ([] if publicados else ["SEM_INDICADORES_PUBLICADOS"])
        cruzam = self._cruzam_virada()
        if cruzam:
            motivos.append(f"{cruzam} indicador(es) com janela na virada do mês, fora da comparação")
        return {"completo": not motivos, "mes_encerrado": fechado,
                "dias_no_mes": len(self.dias), "dias_sem_brutos": dias_sem_brutos,
                "motivos": motivos}

    # -- base fixada pela US12-T03 -------------------------------------------
    def _base(self):
        id_maximo = self._brutos().with_entities(func.max(RegistroBrutoGPS.id)).scalar()
        base = {"bruto_id_maximo_no_mes": id_maximo, "reprocessamento_us12_t03": None}
        if not self.reprocessar_brutos:
            return base
        manifesto = criar_execucao_reprocessamento(
            periodo_inicio=self.inicio, periodo_fim=self.fim,
            versao_base=self.versoes["base"], versao_codigo=self.versoes["codigo"],
            versao_regra=self.versoes["regra"],
        )
        tratados = executar_reprocessamento(manifesto.id)
        validos = ResultadoReprocessamento.query.filter_by(
            execucao_processamento_id=manifesto.id, valida=True).count()
        esperados = self._brutos().filter(
            RegistroBrutoGPS.id <= manifesto.bruto_id_maximo).count()
        base["reprocessamento_us12_t03"] = {
            "execucao_id": manifesto.id, "chave_logica": manifesto.chave_logica,
            "bruto_id_maximo": manifesto.bruto_id_maximo, "estado": manifesto.estado,
            "brutos_no_snapshot": esperados, "tratados": tratados, "validos": validos,
            "invalidos": tratados - validos, "confere": tratados == esperados,
        }
        return base

    # -- comparação ----------------------------------------------------------
    def _valor_igual(self, publicado, recalculado):
        if publicado == recalculado:
            return "IGUAL"
        if None in (publicado, recalculado) or self.tolerancia is None:
            return "DIVERGENTE"
        return "IGUAL_COM_TOLERANCIA" if abs(publicado - recalculado) <= self.tolerancia + 1e-9 else "DIVERGENTE"

    def _comparar(self, registro):
        item = {"id": registro.id, "indicador": registro.indicador, "linha": registro.linha_codigo,
                "sentido": registro.sentido, "inicio": _iso(registro.periodo_inicio),
                "fim": _iso(registro.periodo_fim), "unidade": registro.unidade,
                "valor_publicado": registro.valor,
                "contagens_publicadas": [registro.contagem_numerador, registro.contagem_denominador],
                "versao_calculo": registro.versao_calculo,
                "valor_nulo": registro.valor is None}
        calculo = CALCULOS.get(registro.indicador)
        if calculo is None:
            return {**item, "status": "NAO_SUPORTADO"}
        funcao, versao_atual = calculo
        if registro.versao_calculo != versao_atual:
            return {**item, "status": "VERSAO_INCOMPATIVEL",
                    "detalhe": f"publicado com {registro.versao_calculo}; código atual {versao_atual}"}
        reconstrucao = (registro.qualidade or {}).get("versao_reconstrucao")
        if not reconstrucao or not registro.versao_correspondencia:
            return {**item, "status": "DADOS_INSUFICIENTES",
                    "detalhe": "versão de reconstrução ou correspondência não registrada"}
        try:
            novo = funcao(linha=registro.linha_codigo, sentido=registro.sentido,
                          inicio=registro.periodo_inicio, fim=registro.periodo_fim,
                          versao_gtfs_id=registro.versao_gtfs_id,
                          versao_correspondencia=registro.versao_correspondencia,
                          versao_reconstrucao=reconstrucao, versao_calculo=versao_atual)
        except ValueError as erro:
            return {**item, "status": "ERRO_RECALCULO", "detalhe": str(erro)}
        capturas = (registro.qualidade or {}).get("capturas_programacao")
        if capturas is not None and capturas != (novo["qualidade"] or {}).get("capturas_programacao"):
            return {**item, "status": "VERSAO_INCOMPATIVEL",
                    "detalhe": "programação substituída: capturas usadas no recálculo diferem das publicadas"}
        divergencias = [
            {"campo": c, "publicado": _iso(getattr(registro, c)), "recalculado": _iso(novo[c])}
            for c in CAMPOS if c != "valor" and getattr(registro, c) != novo[c]
        ]
        status_valor = self._valor_igual(registro.valor, novo["valor"])
        if status_valor == "DIVERGENTE":
            divergencias.append({"campo": "valor", "publicado": registro.valor,
                                 "recalculado": novo["valor"]})
        status = "DIVERGENTE" if divergencias else status_valor
        return {**item, "status": status, "valor_recalculado": novo["valor"],
                "contagens_recalculadas": [novo["contagem_numerador"], novo["contagem_denominador"]],
                "divergencias": divergencias}

    # -- execução --------------------------------------------------------------
    def executar(self):
        inspetor = inspect(db.engine)
        ausentes = [t for t in ("registros_gps", "viagens_observadas", "indicadores_operacionais")
                    if not inspetor.has_table(t)]
        if ausentes:
            return {"tarefa": "US12-T04", "mes": self.mes, "versoes": self.versoes,
                    "quantidade_indicadores": 0, "veredito": "INCONCLUSIVO",
                    "aceite_integral": False,
                    "completude": {"completo": False,
                                   "motivos": [f"TABELA_AUSENTE:{t}" for t in ausentes]},
                    "limitacoes": ["Base sem as tabelas necessárias (migrações pendentes)."]}
        antes = self._impressao()
        publicados = self._publicados().all()
        completude = self._completude(publicados)
        base = self._base()
        itens = [self._comparar(registro) for registro in publicados]
        if db.session.new or db.session.dirty or db.session.deleted:
            db.session.rollback()
            raise RuntimeError("recálculo tentou alterar a sessão; nada foi gravado")
        db.session.expire_all()  # relê a base, não o estado em memória
        depois = self._impressao()

        contagem = {s: sum(i["status"] == s for i in itens) for s in (
            "IGUAL", "IGUAL_COM_TOLERANCIA", "DIVERGENTE", "VERSAO_INCOMPATIVEL",
            "DADOS_INSUFICIENTES", "NAO_SUPORTADO", "ERRO_RECALCULO")}
        por_indicador = {}
        for i in itens:
            grupo = por_indicador.setdefault(i["indicador"], {"publicados": 0, "reproduzidos": 0})
            grupo["publicados"] += 1
            grupo["reproduzidos"] += i["status"] in ("IGUAL", "IGUAL_COM_TOLERANCIA")
        preservado = antes == depois
        reprocesso = base["reprocessamento_us12_t03"]
        falhas = sum(contagem[s] for s in ("DIVERGENTE", "VERSAO_INCOMPATIVEL", "ERRO_RECALCULO")) \
            or not preservado or (reprocesso is not None and not reprocesso["confere"])
        reproduzivel = completude["completo"] and contagem["IGUAL"] + contagem["IGUAL_COM_TOLERANCIA"] == len(itens)
        veredito = "NAO_REPRODUZIDO" if falhas else "REPRODUZIDO" if reproduzivel else "INCONCLUSIVO"

        limitacoes = []
        if not RECONSTRUCAO_A_PARTIR_DOS_BRUTOS:
            limitacoes.append("Recálculo parte de viagens_observadas: nenhuma rotina reconstrói "
                              "ViagemObservada a partir dos brutos (US14-T01C não integrada).")
        if self.tolerancia is None:
            limitacoes.append("Tolerância numérica não acordada: comparação por igualdade exata; "
                              "definição pendente com o PO.")
        if reprocesso is None:
            limitacoes.append("Base fixada só por leitura (id máximo e hash dos brutos); "
                              "reprocessamento US12-T03 não executado.")
        if self.dados_sinteticos:
            limitacoes.append("Dados sintéticos de teste: não comprovam o aceite com dados reais.")
        return {
            "tarefa": "US12-T04",
            "mes": self.mes, "fuso": str(FUSO),
            "periodo_utc": {"inicio": self.inicio.isoformat(), "fim_exclusivo": self.fim.isoformat()},
            "origem_dados": "SINTETICA" if self.dados_sinteticos else "BASE_CONFIGURADA",
            "versoes": {**self.versoes, "algoritmos": {t: v for t, (_, v) in CALCULOS.items()}},
            "tolerancia": {"valor_absoluta": self.tolerancia,
                           "modo": "IGUALDADE_EXATA" if self.tolerancia is None else "ABSOLUTA",
                           "aplica_se_a": "valor (contagens, versões e períodos sempre exatos)"},
            "completude": completude, "base": base,
            "quantidade_indicadores": len(itens), "resultado": contagem,
            "por_indicador": por_indicador,
            "preservacao": {"inalterado": preservado, "antes": antes, "depois": depois},
            "itens": itens, "veredito": veredito,
            "aceite_integral": (veredito == "REPRODUZIDO" and not self.dados_sinteticos
                                and RECONSTRUCAO_A_PARTIR_DOS_BRUTOS),
            "limitacoes": limitacoes,
        }


def _versao_codigo():
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                                text=True, check=True).stdout.strip()
        sujo = subprocess.run(["git", "status", "--porcelain"], capture_output=True,
                              text=True, check=True).stdout.strip()
        return f"git:{commit}" + ("-dirty" if sujo else "")
    except (OSError, subprocess.CalledProcessError):
        return None


def _versao_base():
    if not inspect(db.engine).has_table("alembic_version"):
        return None
    return "alembic:" + str(db.session.execute(text("SELECT version_num FROM alembic_version")).scalar())


def main(argv=None):
    from app import create_app

    parser = argparse.ArgumentParser(description="US12-T04: reprodução de um mês de indicadores")
    parser.add_argument("--mes", required=True, help="AAAA-MM, em America/Sao_Paulo")
    parser.add_argument("--versao-base", help="padrão: revisão Alembic da base")
    parser.add_argument("--versao-codigo", help="padrão: commit git atual")
    parser.add_argument("--versao-regra", default=VERSAO_REGRA)
    parser.add_argument("--tolerancia", type=float,
                        help="tolerância absoluta acordada com o PO; omitida = igualdade exata")
    parser.add_argument("--reprocessar-brutos", action="store_true",
                        help="executa o manifesto US12-T03 (grava em execucoes_processamento)")
    parser.add_argument("--saida", type=Path, help="arquivo JSON do relatório de evidências")
    args = parser.parse_args(argv)
    app = create_app()
    with app.app_context():
        if not args.reprocessar_brutos and db.engine.dialect.name == "mysql":
            db.session.execute(text("SET SESSION TRANSACTION READ ONLY"))
        try:
            relatorio = ReproducaoIndicadores(
                args.mes, versao_base=args.versao_base or _versao_base(),
                versao_codigo=args.versao_codigo or _versao_codigo(),
                versao_regra=args.versao_regra, tolerancia_valor=args.tolerancia,
                reprocessar_brutos=args.reprocessar_brutos,
            ).executar()
        finally:
            db.session.rollback()
    conteudo = json.dumps(relatorio, ensure_ascii=False, indent=2, default=str)
    if args.saida:
        args.saida.write_text(conteudo + "\n", encoding="utf-8")
    print(conteudo)
    return {"REPRODUZIDO": 0, "NAO_REPRODUZIDO": 1}.get(relatorio["veredito"], 2)


if __name__ == "__main__":
    sys.exit(main())
