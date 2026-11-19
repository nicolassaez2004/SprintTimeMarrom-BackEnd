"""Ciclo durável e worker singleton da US29; execução fora do servidor Flask."""

import hashlib
import json
import logging
import threading
import time
from datetime import timedelta
from math import ceil
from pathlib import Path

from filelock import FileLock
from filelock import Timeout as ArquivoOcupado
from flask import current_app
from sqlalchemy.exc import DBAPIError

from app import db
from app.monitoramento.journal import JournalColeta, instante
from app.repositories.coleta import ColetaRepository, PossePerdida

from .retentativas import ESPERAS, MAX_TENTATIVAS, causa_segura, temporaria

logger = logging.getLogger(__name__)


def diretorio_journal():
    nome = hashlib.sha256(
        f"{db.engine.url.host}/{db.engine.url.database}".encode()
    ).hexdigest()[:16]
    return (
        Path(
            current_app.config.get("COLETA_STATE_DIR")
            or Path(current_app.instance_path) / "coleta"
        )
        / nome
    )


class CicloResiliente:
    def __init__(
        self,
        journal,
        repository,
        cliente,
        *,
        ativo=lambda: True,
        agora=None,
        monotonic=time.monotonic,
        dormir=time.sleep,
    ):
        self.journal = journal
        self.repo = repository
        self.cliente = cliente
        self.ativo = ativo
        self.agora = agora or journal.agora
        self.monotonic = monotonic
        self.dormir = dormir

    def falha(self, erro, inicio=None):
        id = self.journal.abrir_lacuna(causa_segura(erro), inicio)
        c = self.journal.dados["ciclo"]
        if c is not None:
            self.journal.atualizar_ciclo(lacuna_id=id, erro=causa_segura(erro))
        self.journal.verificar_alerta()

    def _posse(self):
        if not self.ativo():
            raise PossePerdida("Lock do coletor perdido")

    def coletar_ciclo(self, prevista_em=None):
        retomada = self.journal.dados["ciclo"] is not None
        c = self.journal.iniciar_ciclo(prevista_em)
        inicio_mono = self.monotonic()
        try:
            self._posse()
            self.repo.inicio(c, self.journal.snapshot())
            while c["resposta"] is None and not c["final_falha"]:
                if c["tentativas"] >= MAX_TENTATIVAS:
                    self.journal.atualizar_ciclo(final_falha=True)
                    break
                self._posse()
                if c.get("retentar_em"):
                    restante = (
                        instante(c["retentar_em"]) - self.agora()
                    ).total_seconds()
                    if restante > 0:
                        self.dormir(restante)
                    self.journal.atualizar_ciclo(retentar_em=None)
                    self.journal.verificar_alerta()
                self._posse()
                inicio_tentativa = self.agora()
                self.journal.atualizar_ciclo(tentativas=c["tentativas"] + 1)
                cliente = None
                try:
                    cliente = self.cliente()
                    coletado = self.agora()
                    resposta = cliente.buscar_todas_posicoes()
                    json.dumps(resposta, allow_nan=False)  # validar antes do checkpoint
                    self.journal.atualizar_ciclo(
                        resposta=resposta, coletado_em=coletado.isoformat()
                    )
                except Exception as erro:
                    self.falha(erro, inicio_tentativa)
                    if not temporaria(erro) or c["tentativas"] >= MAX_TENTATIVAS:
                        self.journal.atualizar_ciclo(final_falha=True)
                        break
                    retentar = self.agora() + timedelta(
                        seconds=ESPERAS[c["tentativas"] - 1]
                    )
                    self.journal.atualizar_ciclo(retentar_em=retentar.isoformat())
                finally:
                    fechar = getattr(cliente, "fechar", None)
                    if callable(fechar):
                        try:
                            fechar()
                        except Exception:
                            logger.error("Falha ao fechar sessão da fonte")
            self._posse()
            total = self.repo.concluir(c, self.journal.snapshot(), self.agora())
            if not c["final_falha"]:
                if c["confirmada_em"] is None:
                    confirmada = self.agora().isoformat()
                    segundos = (
                        (instante(confirmada) - instante(c["inicio"])).total_seconds()
                        if retomada
                        else self.monotonic() - inicio_mono
                    )
                    self.journal.atualizar_ciclo(
                        confirmada_em=confirmada,
                        duracao_ms=max(0, ceil(segundos * 1000)),
                    )
                self.journal.recuperar(self.agora())
                self._posse()
                self.repo.confirmar(c, self.journal.snapshot())
            self.journal.encerrar_ciclo()
            logger.info(
                "Ciclo %s: %s, tentativas=%d, novos=%d",
                c["chave"],
                "FALHA" if c["final_falha"] else "OK",
                c["tentativas"],
                total,
            )
            return total
        except Exception as erro:
            db.session.rollback()
            self.falha(erro)
            if isinstance(erro, DBAPIError) and not temporaria(erro):
                from app.monitoramento.journal import publicar

                self.journal.atualizar_ciclo(final_falha=True)
                # Preserva a resposta que não pôde entrar no esquema; a lacuna
                # não é encerrada por um lote rejeitado.
                publicar(
                    self.journal.diretorio / "rejeitados" / (c["chave"] + ".json"), c
                )
                self._posse()
                self.repo.concluir(c, self.journal.snapshot(), self.agora())
                self.journal.encerrar_ciclo()
                return 0
            raise


def executar_worker(
    *,
    max_ciclos=None,
    agora=None,
    dormir=time.sleep,
    retornar_total=False,
    prevista_em=None,
):
    from app.coleta.bus_collector import (
        BusCollector,
        LockColetor,
        SPTransClient,
        executar_coletor,
    )

    pasta = diretorio_journal()
    pasta.mkdir(parents=True, exist_ok=True)
    try:
        with FileLock(pasta / "worker.lock", timeout=0):
            journal = JournalColeta(pasta, **({"agora": agora} if agora else {}))
            parar = threading.Event()
            monitor = threading.Thread(
                target=journal.vigiar, args=(parar,), daemon=True
            )
            monitor.start()
            reconexoes = 0
            try:
                while True:
                    lock = LockColetor(db.engine)
                    try:
                        if not lock.adquirir():
                            if retornar_total:
                                raise PossePerdida("Outro coletor possui o lock MySQL")
                            logger.error("Outro coletor possui o lock MySQL")
                            return 1
                        repo = ColetaRepository()
                        repo.assumir()
                        intervalo = current_app.config.get(
                            "GPS_COLLECTION_INTERVAL_SECONDS", 30
                        )
                        repo.restaurar(journal, intervalo, journal.agora())
                        ciclo = CicloResiliente(
                            journal,
                            repo,
                            SPTransClient,
                            ativo=lock.ativo,
                            dormir=dormir,
                        )
                        resultado = [0]
                        coletor = BusCollector(servico=ciclo)

                        def coletar(prevista, servico=coletor, saida=resultado):
                            nonlocal reconexoes
                            saida[0] = servico.coletar_ciclo(
                                prevista_em if retornar_total else prevista
                            )
                            reconexoes = 0
                            return saida[0]

                        executar_coletor(
                            intervalo,
                            coletar=coletar,
                            manter_ativo=None,
                            max_ciclos=max_ciclos,
                            dormir=dormir,
                            propagar_erros=True,
                        )
                        return resultado[0] if retornar_total else 0
                    except KeyboardInterrupt:
                        return 0
                    except Exception as erro:
                        db.session.rollback()
                        journal.abrir_lacuna(causa_segura(erro))
                        journal.verificar_alerta()
                        logger.error("Coletor aguarda retomada: %s", causa_segura(erro))
                        if isinstance(erro, PossePerdida) and retornar_total:
                            raise
                        if not temporaria(erro) and not isinstance(erro, PossePerdida):
                            raise
                        dormir(min(2 ** min(reconexoes, 5), 30))
                        reconexoes += 1
                    finally:
                        lock.liberar()
                        db.session.remove()
            finally:
                parar.set()
                monitor.join(timeout=2)
    except ArquivoOcupado:
        if retornar_total:
            raise PossePerdida(
                "Outro processo utiliza o checkpoint da coleta"
            ) from None
        logger.error("Outro processo utiliza o checkpoint da coleta")
        return 1
