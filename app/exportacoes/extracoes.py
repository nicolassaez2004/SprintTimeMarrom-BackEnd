"""US28-T01: ExportService — registra pedidos de extração de brutos sem gerar o arquivo.

Fluxo 3 do diagrama de componentes: ``POST /extracoes`` grava ``PedidoExtracao``
e o seu ``ExecucaoProcessamento`` (tipo ``EXTRACAO_BRUTOS``) numa só transação,
pelo ``JobService`` da US20 — a fila é a própria tabela de jobs no MySQL, então
o pedido confirmado já é o trabalho pendente, com ou sem worker no ar. O
arquivo é gerado depois, pelo worker (tarefa seguinte da US28).
"""
import json
import re
from datetime import datetime, timedelta, timezone
from hashlib import sha256

from flask import current_app
from sqlalchemy import func, or_

from app import db
from app.jobs import JobService
from app.models import ExecucaoProcessamento, PedidoExtracao, RegistroBrutoGPS

TIPO = "EXTRACAO_BRUTOS"
FORMATOS = ("CSV", "PARQUET")
VERSAO_REGRA = "extracao-brutos-v1"
# US28: "arquivo de um mês de uma linha"; teto a confirmar pelo PO.
PERIODO_MAXIMO = timedelta(days=31)
# Período precisa estar fechado: registros chegam em até 60 s (US29); a mesma
# folga da exportação diária (US09) evita extrair um período ainda incompleto.
MARGEM_FECHAMENTO = timedelta(minutes=10)
_CAMPOS = {"linha", "inicio", "fim", "formato"}
_LINHA = re.compile(r"[0-9]{1,16}")  # código Olho Vivo (cl) = RegistroBrutoGPS.linha_origem


class PedidoInvalido(ValueError):
    def __init__(self, campo, mensagem):
        self.campo = campo
        super().__init__(mensagem)


def _iso(instante):
    return instante.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _instante(dados, campo):
    valor = dados.get(campo)
    try:
        instante = datetime.fromisoformat(valor) if isinstance(valor, str) else None
    except ValueError:
        instante = None
    if instante is None or instante.utcoffset() is None:
        raise PedidoInvalido(campo, "Informe data e hora ISO 8601 com fuso (ex.: 2026-11-01T00:00:00-03:00).")
    return instante.astimezone(timezone.utc)


class ExportService:
    def __init__(self, jobs=None, relogio=None):
        self.jobs = jobs or JobService()
        self.relogio = relogio or (lambda: datetime.now(timezone.utc))

    def validar(self, dados):
        """Normaliza o corpo do pedido; ``PedidoInvalido`` antes de qualquer gravação."""
        if not isinstance(dados, dict):
            raise PedidoInvalido(None, "Envie um objeto JSON com linha, inicio, fim e formato.")
        extras = sorted(set(dados) - _CAMPOS)
        if extras:
            raise PedidoInvalido(extras[0], "Campo não reconhecido.")
        linha = dados.get("linha")
        if not isinstance(linha, str) or not _LINHA.fullmatch(linha):
            raise PedidoInvalido("linha", "Informe o código numérico da linha na Olho Vivo (campo cl).")
        formato = dados.get("formato")
        if not isinstance(formato, str) or formato.upper() not in FORMATOS:
            raise PedidoInvalido("formato", "Formato deve ser CSV ou PARQUET.")
        inicio, fim = _instante(dados, "inicio"), _instante(dados, "fim")
        if fim <= inicio:
            raise PedidoInvalido("fim", "O fim do período deve ser posterior ao início.")
        if fim - inicio > PERIODO_MAXIMO:
            raise PedidoInvalido("fim", f"Período máximo de {PERIODO_MAXIMO.days} dias por pedido.")
        if fim > self.relogio() - MARGEM_FECHAMENTO:
            raise PedidoInvalido("fim", "O período deve estar encerrado há pelo menos 10 minutos.")
        return {"linha": linha, "inicio": inicio, "fim": fim, "formato": formato.upper()}

    def _chave(self, pedido):
        """Mesma linha, período e formato reutilizam o job vivo; só um job com
        ERRO sem tentativas restantes abre uma nova geração ``base:n``."""
        identidade = {"tipo": TIPO, "regra": VERSAO_REGRA, "linha": pedido["linha"],
                      "inicio": _iso(pedido["inicio"]), "fim": _iso(pedido["fim"]),
                      "formato": pedido["formato"]}
        base = "extracao-us28:" + sha256(json.dumps(
            identidade, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        E = ExecucaoProcessamento
        jobs = (E.query.filter(or_(E.chave_logica == base, E.chave_logica.like(base + ":%")))
                .order_by(E.criada_em.desc(), E.id.desc()).all())
        vivos = [j for j in jobs if not (j.estado == "ERRO" and j.tentativas >= self.jobs.limite)]
        if vivos:
            return vivos[0].chave_logica
        return f"{base}:{len(jobs)}" if jobs else base

    def solicitar(self, dados):
        """Valida, então grava pedido + job numa transação (``JobService.solicitar``).

        Repetição idempotente: devolve o pedido já ligado ao job reutilizado.
        Falha em qualquer gravação desfaz ambos.
        """
        pedido = self.validar(dados)
        snapshot = db.session.query(func.max(RegistroBrutoGPS.id)).scalar() or 0
        versao_base = f"registros_gps:id<={snapshot}"
        gravado = {}

        def vincular(jobs):
            job = jobs[0]
            existente = PedidoExtracao.query.filter_by(execucao_processamento_id=job.id).one_or_none()
            gravado["pedido"] = existente or self._novo(pedido, job)

        self.jobs.solicitar([dict(
            tipo=TIPO, chave_logica=self._chave(pedido),
            periodo_inicio=pedido["inicio"], periodo_fim=pedido["fim"],
            bruto_id_maximo=snapshot, versao_base=versao_base, versao_regra=VERSAO_REGRA,
            versao_codigo=current_app.config.get("VERSAO_CODIGO") or "nao-informada",
            parametros={"linha": pedido["linha"], "formato": pedido["formato"],
                        "periodo": {"inicio": _iso(pedido["inicio"]), "fim_exclusivo": _iso(pedido["fim"])},
                        "snapshot_bruto_id_maximo": snapshot},
        )], vincular=vincular)
        return gravado["pedido"]

    @staticmethod
    def _novo(pedido, job):
        novo = PedidoExtracao(linha_codigo=pedido["linha"], inicio_periodo=pedido["inicio"],
                              fim_periodo=pedido["fim"], formato=pedido["formato"],
                              versao_base=job.versao_base, execucao_processamento_id=job.id)
        db.session.add(novo)
        db.session.flush()
        return novo

    @staticmethod
    def obter(pedido_id):
        return db.session.get(PedidoExtracao, pedido_id)

    def descrever(self, pedido):
        """Representação pública: sem chave lógica, token, caminho ou id interno do job."""
        estado = pedido.estado_publico(self.jobs.limite)
        return {
            "id": pedido.id,
            "estado": estado,
            "linha": pedido.linha_codigo,
            "periodo": {"inicio": _iso(pedido.inicio_periodo), "fim": _iso(pedido.fim_periodo)},
            "formato": pedido.formato,
            "solicitado_em": _iso(pedido.criado_em),
            "versao_base": pedido.versao_base,
            "erro": (pedido.execucao.erro or "ARQUIVO_INDISPONIVEL") if estado == "ERRO" else None,
            "consulta": f"/api/extracoes/{pedido.id}",
        }
