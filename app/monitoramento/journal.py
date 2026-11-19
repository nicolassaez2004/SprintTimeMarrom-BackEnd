"""Checkpoint local durável: funciona inclusive durante indisponibilidade SQL.

Requer o FileLock do worker para exclusão entre processos no mesmo host. O
RLock protege o monitor local; nenhuma thread secundária coleta posições.
"""

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4, uuid5


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


class JournalColeta:
    def __init__(self, diretorio, *, agora=utc):
        self.diretorio = Path(diretorio)
        self.caminho = self.diretorio / "estado.json"
        self.agora = agora
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
                return abertas[0]["id"]
            lacuna = dict(
                id=str(uuid4()),
                inicio=(inicio or self.agora()).isoformat(),
                fim=None,
                causa=causa,
                fonte="OLHO_VIVO",
            )
            self.dados["lacunas"].append(lacuna)
            self.salvar()
            return lacuna["id"]

    def _alerta(self, lacuna, tipo, agora):
        id = str(uuid5(UUID(lacuna["id"]), tipo))
        if any(a["id"] == id for a in self.dados["alertas"]):
            return
        evento = dict(
            id=id,
            lacuna_id=lacuna["id"],
            tipo=tipo,
            criada_em=agora.isoformat(),
            enviada_em=agora.isoformat(),
            canal="CAIXA_INTERNA",
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
            for lacuna in self.dados["lacunas"]:
                if (
                    lacuna["fim"] is None
                    and (agora - instante(lacuna["inicio"])).total_seconds() >= 600
                ):
                    self._alerta(lacuna, "FALHA", agora)
            if len(self.dados["alertas"]) != anteriores:
                self.salvar()

    def recuperar(self, confirmada_em):
        with self.mutex:
            for lacuna in self.dados["lacunas"]:
                if lacuna["fim"] is None:
                    # Se o primeiro retorno só ocorre depois de 600 s, registrar
                    # a falha antes da recuperação, mesmo sem tick intermediário.
                    if (
                        confirmada_em - instante(lacuna["inicio"])
                    ).total_seconds() >= 600:
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
            except OSError:
                # Não transforma falha de disco em alerta enviado. O worker
                # também verifica o checkpoint e irá falhar explicitamente.
                pass
