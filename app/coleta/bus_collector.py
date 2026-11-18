"""Coleta periódica das posições de toda a frota (BusCollector)."""

import logging
import argparse
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app import create_app, db
from app.config import validar_intervalo_coleta
from app.integrations.sptrans import SPTransClient
from app.models import ExecucaoColeta, RegistroBrutoGPS, Veiculo
from app.coleta.qualidade import validar_registro_gps, validar_unicidade_chave


logger = logging.getLogger(__name__)
INTERVALO_COLETA_SEGUNDOS = 30
NOME_LOCK_COLETOR = "sptrans_gps_coletor"  # MySQL GET_LOCK


def _ler_coordenada(valor, limite):
    """Converte a coordenada da API; ausente, inválida ou fora da faixa vira NULL."""
    try:
        coordenada = float(valor)
    except (TypeError, ValueError):
        return None
    return coordenada if -limite <= coordenada <= limite else None


def _ler_horario_api(valor):
    """Converte o campo `ta` (ISO 8601 em UTC) para datetime UTC; inválido vira NULL."""
    if not valor:
        return None
    try:
        horario = datetime.fromisoformat(str(valor).replace("Z", "+00:00"))
    except ValueError:
        return None
    if horario.tzinfo is None:
        # A API Olho Vivo publica `ta` em UTC.
        horario = horario.replace(tzinfo=timezone.utc)
    return horario.astimezone(timezone.utc)


def _ler_sentido(valor):
    """Converte `sl` da API (1 ou 2) para texto; qualquer outro valor vira NULL.

    Na API Olho Vivo, `sl` é o sentido de operação da linha: 1 = terminal
    principal -> secundário; 2 = secundário -> principal.
    """
    if isinstance(valor, bool):
        return None
    if isinstance(valor, int) and valor in (1, 2):
        return str(valor)
    if isinstance(valor, str) and valor.strip() in ("1", "2"):
        return valor.strip()
    return None


class BusCollector:
    """Conduz um ciclo de coleta: consulta a SPTrans e grava o bruto e a execução.

    Cada ciclo lê as posições de toda a frota (`GET /Posicao`) em uma única
    consulta. A comunicação com a API fica em `SPTransClient`; esta classe não
    faz HTTP. O início do ciclo é gravado antes da consulta, para que um ciclo
    que falhe no meio continue contabilizado em `execucoes_coleta` com estado
    `FALHA`.
    """

    def coletar_ciclo(self, prevista_em=None):
        iniciado_em = datetime.now(timezone.utc)
        execucao = ExecucaoColeta(
            fonte="OLHO_VIVO",
            prevista_em=prevista_em,
            iniciada_em=iniciado_em,
            estado="EM_ANDAMENTO",
            tentativas=1,
            recebidos=0,
            novos_persistidos=0,
        )
        db.session.add(execucao)
        db.session.commit()

        try:
            cliente = SPTransClient()
            resposta = cliente.buscar_todas_posicoes()
            recebidos = self._ler_frota(resposta, execucao.id, iniciado_em)
            novos = self._descartar_duplicados(recebidos)
            if novos:
                self._registrar_veiculos_novos(novos)
                db.session.add_all(novos)
            execucao.finalizada_em = datetime.now(timezone.utc)
            execucao.recebidos = len(recebidos)
            execucao.novos_persistidos = len(novos)
            execucao.estado = "OK"
            execucao.erro = None
            db.session.commit()
        except Exception as error:
            # Só o tipo do erro é gravado: mensagens da SPTrans podem conter o token.
            logger.exception("Falha no ciclo de coleta GPS")
            db.session.rollback()
            execucao.finalizada_em = datetime.now(timezone.utc)
            execucao.estado = "FALHA"
            execucao.recebidos = 0
            execucao.novos_persistidos = 0
            execucao.erro = type(error).__name__
            db.session.commit()
            return 0

        logger.info(
            "Ciclo concluído: %d posições recebidas, %d novas gravadas.",
            len(recebidos), len(novos),
        )
        return len(novos)

    @staticmethod
    def _ler_frota(resposta, execucao_id, iniciado_em):
        """Converte a resposta de `GET /Posicao` em registros brutos.

        Formato: `{"hr", "l": [{"c", "cl", "sl", "lt0", "lt1", "qv", "vs": [...]}]}`.
        Entradas fora do formato são ignoradas sem interromper o ciclo.
        """
        registros = []
        for linha in resposta.get("l") or []:
            if not isinstance(linha, dict):
                continue
            codigo_linha = linha.get("cl")
            linha_origem = None if codigo_linha is None else str(codigo_linha)
            sentido = _ler_sentido(linha.get("sl"))
            veiculos = linha.get("vs")
            if not isinstance(veiculos, list):
                continue

            for veiculo in veiculos:
                if not isinstance(veiculo, dict):
                    continue
                prefixo = veiculo.get("p")
                prefixo = None if prefixo is None else str(prefixo)

                # O objeto original vai no payload; as colunas tipadas são uma
                # leitura dele, com NULL para ausentes ou inválidos.
                registros.append(
                    validar_registro_gps(RegistroBrutoGPS(
                        execucao_id=execucao_id,
                        veiculo_prefixo=prefixo,
                        prefixo_origem=prefixo,
                        linha_origem=linha_origem,
                        sentido_origem=sentido,
                        latitude=_ler_coordenada(veiculo.get("py"), 90),
                        longitude=_ler_coordenada(veiculo.get("px"), 180),
                        horario_gps=_ler_horario_api(veiculo.get("ta")),
                        coletado_em=iniciado_em,
                        payload=veiculo,
                    ))
                )
        return registros

    @staticmethod
    def _registrar_veiculos_novos(registros):
        """Cadastra em `veiculos` os prefixos ainda desconhecidos (FK do bruto)."""
        prefixos = {r.veiculo_prefixo for r in registros if r.veiculo_prefixo is not None}
        if not prefixos:
            return
        existentes = {
            linha[0]
            for linha in db.session.query(Veiculo.prefixo)
            .filter(Veiculo.prefixo.in_(prefixos))
            .all()
        }
        for prefixo in sorted(prefixos - existentes):
            db.session.add(Veiculo(prefixo=prefixo))

    @staticmethod
    def _descartar_duplicados(registros):
        """Descarta duplicatas apenas quando a chave GPS está completa."""
        return validar_unicidade_chave(registros, sessao=db.session)


def executar_coletor(
    intervalo_segundos=None,
    *,
    coletar=None,
    relogio=None,
    dormir=None,
    limpar_sessao=None,
    manter_ativo=None,
    max_ciclos=None,
):
    """Roda ciclos em cadência fixa; relógio/espera são injetáveis para testes.

    `manter_ativo` é chamado antes de cada ciclo; se retornar False (ex.: o lock
    singleton foi perdido), o coletor para para não rodar em duplicidade.
    """
    intervalo = validar_intervalo_coleta(
        INTERVALO_COLETA_SEGUNDOS if intervalo_segundos is None else intervalo_segundos
    )
    coletar = coletar or BusCollector().coletar_ciclo
    relogio = relogio or time.monotonic
    dormir = dormir or time.sleep
    limpar_sessao = limpar_sessao or db.session.remove

    proxima_execucao = relogio()
    ciclos = 0
    while max_ciclos is None or ciclos < max_ciclos:
        espera = proxima_execucao - relogio()
        if espera > 0:
            dormir(espera)

        if manter_ativo is not None and not manter_ativo():
            logger.error("Lock do coletor perdido; encerrando para evitar duplicidade.")
            break

        # Instante planejado para o ciclo, em UTC (ExecucaoColeta.prevista_em).
        atraso = max(relogio() - proxima_execucao, 0)
        prevista_em = datetime.now(timezone.utc) - timedelta(seconds=atraso)
        try:
            coletar(prevista_em)
        except Exception:
            db.session.rollback()
            logger.exception("Falha no ciclo de coleta GPS")
        finally:
            # Garante uma sessão SQL nova a cada execução, mesmo após erro.
            limpar_sessao()

        ciclos += 1
        proxima_execucao += intervalo
        agora = relogio()
        if proxima_execucao <= agora:
            # Não sobrepõe ciclos nem faz catch-up se a chamada demorou demais.
            proxima_execucao = agora + intervalo

    return ciclos


class LockColetor:
    """Lock nomeado no banco que garante um único processo coletor.

    O lock pertence à conexão: se o processo morrer, o banco o libera. Por isso
    a conexão fica aberta durante toda a execução e é verificada a cada ciclo,
    o que também evita que o MySQL a encerre por `wait_timeout`.
    """

    _SQL = {
        "adquirir": "SELECT GET_LOCK(:nome, 0)",
        "verificar": "SELECT IS_USED_LOCK(:nome) = CONNECTION_ID()",
        "liberar": "SELECT RELEASE_LOCK(:nome)",
    }

    def __init__(self, engine):
        if engine.dialect.name != "mysql":
            raise RuntimeError(
                f"O coletor requer MySQL para o lock singleton (atual: {engine.dialect.name})"
            )
        self.engine = engine
        self.conexao = None

    def _executar(self, comando):
        return self.conexao.execute(text(self._SQL[comando]), {"nome": NOME_LOCK_COLETOR}).scalar()

    def adquirir(self):
        self.conexao = self.engine.connect()
        try:
            adquirido = bool(self._executar("adquirir"))
            # Encerra a transação implícita sem soltar o lock (que é de sessão).
            self.conexao.commit()
        except Exception:
            self.liberar()
            raise
        if not adquirido:
            self.liberar()
        return adquirido

    def ativo(self):
        try:
            ativo = bool(self._executar("verificar"))
            self.conexao.commit()
            return ativo
        except Exception:
            logger.exception("Falha verificando o lock do coletor")
            return False

    def liberar(self):
        if self.conexao is None:
            return
        try:
            self._executar("liberar")
            self.conexao.commit()
        except Exception:
            logger.exception("Falha liberando o lock do coletor")
        finally:
            self.conexao.close()
            self.conexao = None


def imprimir_localizacao_atual(prefixo):
    """Imprime a posição atual de um prefixo sem gravar no banco."""
    resposta = SPTransClient().buscar_todas_posicoes()
    linhas = resposta.get("l", []) if isinstance(resposta, dict) else []
    encontrado = False

    for linha in linhas:
        for veiculo in linha.get("vs", []):
            if str(veiculo.get("p")) != str(prefixo):
                continue

            encontrado = True
            print(
                f"Prefixo: {prefixo} | Linha: {linha.get('c')} "
                f"(código {linha.get('cl')}) | "
                f"Latitude: {veiculo.get('py')} | "
                f"Longitude: {veiculo.get('px')} | "
                f"Posição atualizada: {veiculo.get('ta')} | "
                f"Referência da API: {resposta.get('hr')}"
            )

    if not encontrado:
        print(f"Nenhuma posição atual encontrada para o prefixo {prefixo}.")


def imprimir_posicoes_da_linha(termo_linha):
    """Imprime posições atuais dos veículos das linhas encontradas pelo letreiro."""
    cliente = SPTransClient()
    linhas = cliente.buscar_linhas(termo_linha)
    if not linhas:
        print(f"Nenhuma linha encontrada para {termo_linha}.")
        return

    encontrou_veiculo = False
    codigos_consultados = set()
    for linha in linhas:
        codigo_linha = linha.get("cl")
        if codigo_linha is None or codigo_linha in codigos_consultados:
            continue
        codigos_consultados.add(codigo_linha)
        resposta = cliente.buscar_posicoes(codigo_linha)
        for veiculo in resposta.get("vs", []):
            encontrou_veiculo = True
            print(
                f"Linha {linha.get('lt')} (código {codigo_linha}, "
                f"sentido {linha.get('sl')}) | Prefixo: {veiculo.get('p')} | "
                f"Latitude: {veiculo.get('py')} | Longitude: {veiculo.get('px')} | "
                f"Posição atualizada: {veiculo.get('ta')} | "
                f"Referência da API: {resposta.get('hr')}"
            )

    if not encontrou_veiculo:
        print(f"Nenhum veículo localizado agora nas linhas {termo_linha}.")


def main():
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="Coletor de posições GPS da SPTrans")
    diagnostico = parser.add_mutually_exclusive_group()
    diagnostico.add_argument(
        "--localizar",
        metavar="PREFIXO",
        help="imprime a posição atual do prefixo informado e encerra (sem gravar)",
    )
    diagnostico.add_argument(
        "--linha",
        metavar="LETREIRO",
        help="imprime posições atuais dos veículos da linha informada e encerra",
    )
    args = parser.parse_args()

    app = create_app()
    with app.app_context():
        if args.localizar:
            imprimir_localizacao_atual(args.localizar)
        elif args.linha:
            imprimir_posicoes_da_linha(args.linha)
        else:
            lock = LockColetor(db.engine)
            if not lock.adquirir():
                logger.error("Já existe um processo coletor ativo para este banco.")
                return 1
            try:
                executar_coletor(
                    app.config.get(
                        "GPS_COLLECTION_INTERVAL_SECONDS",
                        INTERVALO_COLETA_SEGUNDOS,
                    ),
                    manter_ativo=lock.ativo,
                )
            finally:
                lock.liberar()
                db.session.remove()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
