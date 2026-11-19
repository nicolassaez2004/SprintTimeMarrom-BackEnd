"""Checkpoint local durável: funciona inclusive durante indisponibilidade SQL.

Requer o FileLock do worker para exclusão entre processos no mesmo host. O
RLock protege o monitor local; nenhuma thread secundária coleta posições.
O checkpoint guarda só o pendente (lacunas abertas, alertas não entregues ou
não sincronizados); o histórico fica no MySQL.
"""

import json
import logging
import os
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4, uuid5

logger = logging.getLogger(__name__)

ALERTA_SEGUNDOS = 600
LIMITE_CAUSA = 128  # LacunaColeta.causa


def utc():
    return datetime.now(timezone.utc)


def instante(texto):
    return datetime.fromisoformat(texto).astimezone(timezone.utc)


def publicar(caminho, dados):
    caminho = Path(caminho)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(
        dir=caminho.parent, prefix=".checkpoint-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(dados, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, caminho)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def juntar_causas(*causas):
    """Causas distintas, em ordem, separadas por `;`; cabe na coluna."""
    vistas = []
    for parte in ";".join(c for c in causas if c).split(";"):
        if (
            parte
            and parte not in vistas
            and len(";".join(vistas + [parte])) <= LIMITE_CAUSA
        ):
            vistas.append(parte)
    return ";".join(vistas) or "DESCONHECIDA"


def _acrescentar_causa(lacuna, causa):
    novo = juntar_causas(lacuna["causa"], causa)
    if novo != lacuna["causa"]:
        lacuna["causa"] = novo
        return True
    return False


class JournalColeta:
    def __init__(self, diretorio, *, agora=utc, notificar=None):
        self.diretorio = Path(diretorio)
        self.caminho = self.diretorio / "estado.json"
        self.agora = agora
        # Canal externo opcional; sem ele, a caixa interna é o canal.
        self.notificar = notificar
        self.mutex = threading.RLock()
        self.dados = (
            json.loads(self.caminho.read_text(encoding="utf-8"))
            if self.caminho.exists()
            else dict(
                versao=1, ciclo=None, lacunas=[], alertas=[], ultima_confirmacao=None
            )
        )
        if self.dados.get("versao") != 1:
            raise ValueError("Checkpoint de coleta incompatível")

    def salvar(self):
        with self.mutex:
            publicar(self.caminho, self.dados)

    def atualizar_ciclo(self, **campos):
        with self.mutex:
            self.dados["ciclo"].update(campos)
            self.salvar()

    def importar_lacuna(self, lacuna):
        with self.mutex:
            if not any(l["id"] == lacuna["id"] for l in self.dados["lacunas"]):
                self.dados["lacunas"].append(lacuna)
                self.salvar()

    def snapshot(self):
        with self.mutex:
            return json.loads(json.dumps(self.dados))

    def abrir_lacuna(self, causa, inicio=None):
        with self.mutex:
            abertas = [l for l in self.dados["lacunas"] if l["fim"] is None]
            if abertas:
                if _acrescentar_causa(abertas[0], causa):
                    self.salvar()
                return abertas[0]["id"]
            lacuna = dict(
                id=str(uuid4()),
                inicio=(inicio or self.agora()).isoformat(),
                fim=None,
                causa=juntar_causas(causa),
                fonte="OLHO_VIVO",
            )
            self.dados["lacunas"].append(lacuna)
            self.salvar()
            return lacuna["id"]

    def acrescentar_causa(self, causa):
        """Registra nova causa na lacuna aberta, se houver; não abre lacuna."""
        with self.mutex:
            for lacuna in self.dados["lacunas"]:
                if lacuna["fim"] is None and _acrescentar_causa(lacuna, causa):
                    self.salvar()

    def podar(self, sincronizado):
        """Remove o que o MySQL já confirmou; chamar só após o commit.

        Lacunas abertas e alertas de lacunas abertas ficam, pois controlam a
        deduplicação; um item alterado depois do snapshot também fica.
        """
        with self.mutex:
            lacunas = {l["id"]: l for l in sincronizado["lacunas"]}
            alertas = {a["id"]: a for a in sincronizado["alertas"]}
            manter = [
                l
                for l in self.dados["lacunas"]
                if l["fim"] is None or lacunas.get(l["id"]) != l
            ]
            abertas = {l["id"] for l in manter if l["fim"] is None}
            manter_alertas = [
                a
                for a in self.dados["alertas"]
                if a["enviada_em"] is None
                or a["lacuna_id"] in abertas
                or alertas.get(a["id"]) != a
            ]
            if len(manter) != len(self.dados["lacunas"]) or len(manter_alertas) != len(
                self.dados["alertas"]
            ):
                self.dados["lacunas"] = manter
                self.dados["alertas"] = manter_alertas
                self.salvar()

    def _alerta(self, lacuna, tipo, agora):
        id = str(uuid5(UUID(lacuna["id"]), tipo))
        if any(a["id"] == id for a in self.dados["alertas"]):
            return
        evento = dict(
            id=id,
            lacuna_id=lacuna["id"],
            tipo=tipo,
            criada_em=agora.isoformat(),
            # Caixa interna: o arquivo publicado é a entrega. Canal externo:
            # só após confirmação do destino (entregar_pendentes).
            enviada_em=None if self.notificar else agora.isoformat(),
            canal=getattr(self.notificar, "canal", "CAIXA_INTERNA"),
            tentativas_entrega=0,
            proxima_entrega=None,
            inicio=lacuna["inicio"],
            fim=lacuna["fim"],
            causa=lacuna["causa"],
            mensagem=(
                "Falha contínua da coleta há pelo menos 10 minutos."
                if tipo == "FALHA"
                else "Coleta recuperada e gravação confirmada."
            ),
        )
        # Arquivo publicado antes do checkpoint: após crash, o mesmo ID é
        # republicado no mesmo caminho, em vez de criar outra notificação.
        publicar(self.diretorio / "alertas" / (id + ".json"), evento)
        self.dados["alertas"].append(evento)

    def verificar_alerta(self):
        with self.mutex:
            anteriores = len(self.dados["alertas"])
            agora = self.agora()
            c = self.dados["ciclo"]
            # Falha ainda dentro das retentativas do ciclo também conta.
            if (
                c
                and c.get("falha_inicio")
                and (agora - instante(c["falha_inicio"])).total_seconds()
                >= ALERTA_SEGUNDOS
            ):
                id = self.abrir_lacuna(
                    juntar_causas(*(c.get("causas") or [c["erro"]])),
                    instante(c["falha_inicio"]),
                )
                c["lacuna_id"] = id
            for lacuna in self.dados["lacunas"]:
                if (
                    lacuna["fim"] is None
                    and (agora - instante(lacuna["inicio"])).total_seconds()
                    >= ALERTA_SEGUNDOS
                ):
                    self._alerta(lacuna, "FALHA", agora)
            if len(self.dados["alertas"]) != anteriores:
                self.salvar()

    def entregar_pendentes(self):
        """Envia ao canal externo, fora do mutex; reenvia com espera crescente."""
        if self.notificar is None:
            return
        with self.mutex:
            agora = self.agora()
            pendentes = [
                a
                for a in self.dados["alertas"]
                if a["enviada_em"] is None
                and (
                    a.get("proxima_entrega") is None
                    or instante(a["proxima_entrega"]) <= agora
                )
            ]
        for alerta in pendentes:
            try:
                self.notificar(alerta)
                entregue = True
            except Exception as erro:
                entregue = False
                logger.error("Alerta não entregue: %s", type(erro).__name__)
            with self.mutex:
                agora = self.agora()
                if entregue:
                    alerta["enviada_em"] = agora.isoformat()
                else:
                    n = alerta.get("tentativas_entrega", 0) + 1
                    alerta["tentativas_entrega"] = n
                    alerta["proxima_entrega"] = (
                        agora + timedelta(seconds=min(2**n, 300))
                    ).isoformat()
                publicar(self.diretorio / "alertas" / (alerta["id"] + ".json"), alerta)
                self.salvar()

    def recuperar(self, confirmada_em):
        with self.mutex:
            for lacuna in self.dados["lacunas"]:
                if lacuna["fim"] is None:
                    # Se o primeiro retorno só ocorre depois de 600 s, registrar
                    # a falha antes da recuperação, mesmo sem tick intermediário.
                    if (
                        confirmada_em - instante(lacuna["inicio"])
                    ).total_seconds() >= ALERTA_SEGUNDOS:
                        self._alerta(lacuna, "FALHA", confirmada_em)
                    lacuna["fim"] = confirmada_em.isoformat()
                    if any(
                        a["lacuna_id"] == lacuna["id"] and a["tipo"] == "FALHA"
                        for a in self.dados["alertas"]
                    ):
                        self._alerta(lacuna, "RECUPERACAO", confirmada_em)
            self.dados["ultima_confirmacao"] = confirmada_em.isoformat()
            self.salvar()

    def iniciar_ciclo(self, prevista_em=None):
        with self.mutex:
            if self.dados["ciclo"] is None:
                self.dados["ciclo"] = dict(
                    chave=str(uuid4()),
                    inicio=self.agora().isoformat(),
                    prevista_em=prevista_em.isoformat() if prevista_em else None,
                    tentativas=0,
                    resposta=None,
                    coletado_em=None,
                    erro=None,
                    causas=[],
                    falha_inicio=None,
                    retentar_em=None,
                    final_falha=False,
                    lacuna_id=None,
                    confirmada_em=None,
                    duracao_ms=None,
                )
                self.salvar()
            return self.dados["ciclo"]

    def encerrar_ciclo(self):
        with self.mutex:
            self.dados["ciclo"] = None
            self.salvar()

    def vigiar(self, parar):
        while not parar.wait(1):
            try:
                self.verificar_alerta()
                self.entregar_pendentes()
            except Exception as erro:
                # O monitor não pode morrer: registra o tipo e tenta de novo.
                # Falha de disco não vira alerta enviado; o worker também
                # grava o checkpoint e falhará explicitamente.
                logger.error("Monitor da coleta: %s", type(erro).__name__)
