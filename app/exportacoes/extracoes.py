"""US28: ExportService — pedidos de extração de brutos e geração do arquivo pelo worker.

Fluxo 3 do diagrama de componentes: ``POST /extracoes`` grava ``PedidoExtracao``
e o seu ``ExecucaoProcessamento`` (tipo ``EXTRACAO_BRUTOS``) numa só transação,
pelo ``JobService`` da US20 — a fila é a própria tabela de jobs no MySQL, então
o pedido confirmado já é o trabalho pendente, com ou sem worker no ar.

US28-T02: o worker (``python -m app.jobs --tipo EXTRACAO_BRUTOS``, processo
separado do Flask) executa as duas fases do ``Executor`` da US20:

1. ``gerar`` (posse confirmada pela reserva): grava, com o núcleo da US09
   (``escrever_brutos``), só a linha, o período e o snapshot de brutos do pedido
   num temporário oculto ``.extracao-<job>-<token>.tmp`` do FileStore, lote a
   lote; a cada lote registra o progresso e renova a reserva, desistindo se a
   posse foi perdida. Depois valida o arquivo (linhas e esquema) e calcula o hash.
2. ``publicar`` (job travado ``FOR UPDATE`` com o token): liga o temporário ao
   nome final por conteúdo no FileStore, confere a leitura pelo hash e grava o
   ``ArquivoArmazenado`` no pedido; o executor marca PRONTO no mesmo commit.

O conteúdo é determinístico (snapshot e horário do pedido, sem "agora"): uma
retomada gera os mesmos bytes, o FileStore reconhece o objeto já publicado e o
``ArquivoArmazenado`` é reutilizado pela chave — sem arquivo final duplicado.
"""
import json
import re
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path

import pyarrow as pa
import pyarrow.csv as pacsv
import pyarrow.parquet as pq
from flask import current_app
from sqlalchemy import func, or_

from app import db
from app.exportacoes.parquet import (_SCHEMA, MIME_PARQUET, diretorio_exportacoes,
                                     escrever_brutos, metadados_fonte)
from app.jobs import JobService
from app.models import ArquivoArmazenado, ExecucaoProcessamento, PedidoExtracao, RegistroBrutoGPS
from app.repositories import jobs as repositorio
from app.storage import FileStore, hash_arquivo

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
ARQUIVO = {"PARQUET": ("parquet", MIME_PARQUET), "CSV": ("csv", "text/csv")}
_TEMPORARIO = re.compile(r"\.extracao-([0-9a-f-]{36})-([0-9a-f-]{36})\.tmp")


class ArquivoInvalido(RuntimeError):
    """O arquivo gerado não confere com o que foi escrito; não é publicado."""


def armazenamento():
    """FileStore das extrações, no mesmo volume para publicar por ``os.link``."""
    return FileStore(Path(diretorio_exportacoes()) / "extracoes")


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
    def __init__(self, jobs=None, relogio=None, *, loja=None, tamanho_lote=5000):
        self.jobs = jobs or JobService()
        self.relogio = relogio or (lambda: datetime.now(timezone.utc))
        self.loja, self.tamanho_lote = loja, tamanho_lote

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
        job, arquivo = pedido.execucao, pedido.arquivo
        ultima = (job.historico or [{}])[-1]
        return {
            "id": pedido.id,
            "estado": estado,
            "linha": pedido.linha_codigo,
            "periodo": {"inicio": _iso(pedido.inicio_periodo), "fim": _iso(pedido.fim_periodo)},
            "formato": pedido.formato,
            "solicitado_em": _iso(pedido.criado_em),
            "versao_base": pedido.versao_base,
            "erro": (job.erro or "ARQUIVO_INDISPONIVEL") if estado == "ERRO" else None,
            "tentativas": job.tentativas,
            "progresso": {"registros": ultima.get("registros", 0),
                          "atualizado_em": ultima.get("progresso_em") or ultima.get("fim")},
            # Só com o arquivo completo publicado; o download fica para a T03.
            "arquivo": {"formato": pedido.formato, "tamanho_bytes": arquivo.tamanho_bytes,
                        "sha256": arquivo.sha256} if estado == "PRONTO" else None,
            "consulta": f"/api/extracoes/{pedido.id}",
        }

    # --- Geração pelo worker (US28-T02) ---------------------------------------

    def _loja(self):
        return self.loja or armazenamento()

    def _progresso(self, job_id, token, registros):
        """Registra o progresso na tentativa atual e renova a reserva, numa
        transação curta; sem posse, interrompe a geração (``PossePerdida``)."""
        agora = self.relogio()
        from app.jobs.executor import PossePerdida  # só no worker (rotas não importam o executor)

        job = repositorio.travar_se_dono(job_id, token)
        if job is None:
            db.session.rollback()
            raise PossePerdida(job_id)
        job.reserva_ate = agora + self.jobs.RESERVA
        repositorio.registrar_tentativa(job_id, registros=registros, progresso_em=_iso(agora))
        db.session.commit()

    def _limpar_temporarios(self, loja):
        """Apaga temporários de tentativas que já não têm a posse do seu job."""
        posse = dict(db.session.query(ExecucaoProcessamento.id, ExecucaoProcessamento.reserva_token)
                     .filter(ExecucaoProcessamento.tipo == TIPO,
                             ExecucaoProcessamento.estado == "PROCESSANDO"))
        for caminho in loja.diretorio.glob(".extracao-*.tmp"):
            encontrado = _TEMPORARIO.fullmatch(caminho.name)
            if encontrado and posse.get(encontrado[1]) != encontrado[2]:
                caminho.unlink(missing_ok=True)

    @staticmethod
    def _validar(caminho, formato, total):
        if formato == "PARQUET":
            metadados = pq.read_metadata(caminho)
            linhas, valido = metadados.num_rows, metadados.schema.to_arrow_schema().equals(_SCHEMA)
        else:
            nomes = [c.name for c in _SCHEMA] + ["fonte", "licenca"]
            leitor = pacsv.open_csv(caminho, parse_options=pacsv.ParseOptions(newlines_in_values=True),
                                    convert_options=pacsv.ConvertOptions(
                                        column_types={n: pa.string() for n in nomes}))
            valido = leitor.schema.names == nomes
            linhas = sum(lote.num_rows for lote in leitor)
        if not valido or linhas != total:
            raise ArquivoInvalido(f"{linhas} linhas lidas, {total} escritas")

    def gerar(self, job):
        """Fase 1 do worker: gera e valida o temporário; nada fica publicado."""
        pedido = PedidoExtracao.query.filter_by(execucao_processamento_id=job.id).one()
        loja = self._loja()
        self._limpar_temporarios(loja)
        token = job.reserva_token
        temporario = loja.diretorio / f".extracao-{job.id}-{token}.tmp"
        metadados = {
            **metadados_fonte(),
            b"request_id": pedido.id.encode(),
            b"line": pedido.linha_codigo.encode(),
            b"date_field": b"coletado_em; instantes em UTC",
            b"period_start_utc": pedido.inicio_periodo.isoformat().encode(),
            b"period_end_utc": pedido.fim_periodo.isoformat().encode(),
            b"snapshot_max_id": str(job.bruto_id_maximo).encode(),
            b"requested_at_utc": pedido.criado_em.isoformat().encode(),
        }
        try:
            total = escrever_brutos(
                temporario, pedido.inicio_periodo, pedido.fim_periodo, metadados,
                linha=pedido.linha_codigo, bruto_id_maximo=job.bruto_id_maximo,
                formato=pedido.formato, tamanho_lote=self.tamanho_lote,
                ao_lote=lambda n: self._progresso(job.id, token, n))
            self._validar(temporario, pedido.formato, total)
            self._progresso(job.id, token, total)
            digest, tamanho = hash_arquivo(temporario)
        except Exception:
            temporario.unlink(missing_ok=True)
            raise
        return {"pedido_id": pedido.id, "temporario": temporario, "registros": total,
                "sha256": digest, "tamanho": tamanho}

    def publicar(self, job, gerado):
        """Fase 2, sob ``FOR UPDATE`` com o token: publica e liga o arquivo, sem commit."""
        pedido = db.session.get(PedidoExtracao, gerado["pedido_id"])
        extensao, mime = ARQUIVO[pedido.formato]
        loja = self._loja()
        try:
            chave = loja.publicar_extracao(gerado["temporario"], gerado["sha256"],
                                           gerado["tamanho"], extensao)
        finally:
            gerado["temporario"].unlink(missing_ok=True)
        loja.caminho_extracao(chave, gerado["sha256"])  # legível e íntegro antes do PRONTO
        arquivo = ArquivoArmazenado.query.filter_by(chave=chave).one_or_none()
        if arquivo is None:
            inicio = pedido.inicio_periodo.strftime("%Y%m%dT%H%MZ")
            fim = pedido.fim_periodo.strftime("%Y%m%dT%H%MZ")
            arquivo = ArquivoArmazenado(
                chave=chave, nome_original=f"brutos_linha_{pedido.linha_codigo}_{inicio}_{fim}.{extensao}",
                mime_type=mime, tamanho_bytes=gerado["tamanho"], sha256=gerado["sha256"])
            db.session.add(arquivo)
        pedido.arquivo = arquivo
        repositorio.registrar_tentativa(job.id, registros=gerado["registros"], sha256=gerado["sha256"])


def executor_extracao(app, servico=None, **opcoes):
    """Worker de ``EXTRACAO_BRUTOS``: reserva, gera e publica pelo ``Executor`` da US20."""
    from app.jobs.executor import Executor

    servico = servico or ExportService()
    return Executor(app, TIPO, servico.gerar, servico.publicar, **opcoes)
