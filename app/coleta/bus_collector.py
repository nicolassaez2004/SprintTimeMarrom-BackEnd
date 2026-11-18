"""Coleta periódica das posições dos ônibus selecionados (BusCollector)."""

import logging
import argparse
import json
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app import create_app, db
from app.config import validar_intervalo_coleta
from app.integrations.sptrans import SPTransClient
from app.models import ExecucaoColeta, OnibusSelecionado, RegistroBrutoGPS


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


class BusCollector:
    """Conduz um ciclo de coleta: consulta a SPTrans e grava o bruto e a execução.

    A comunicação com a API fica em `SPTransClient`; esta classe não faz HTTP.
    O início do ciclo é gravado antes da consulta, para que um ciclo que falhe
    no meio continue contabilizado em `execucoes_coleta` com estado `FALHA`.
    """

    def coletar_ciclo(self, prevista_em=None):
        iniciado_em = datetime.now(timezone.utc)
        onibus_selecionados = OnibusSelecionado.query.all()
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

        if not onibus_selecionados:
            execucao.finalizada_em = datetime.now(timezone.utc)
            execucao.estado = "OK"
            db.session.commit()
            logger.info("Nenhum ônibus selecionado; ciclo sem consultas.")
            return 0

        # A API é consultada uma vez por linha, mesmo que vários ônibus selecionados
        # pertençam à mesma linha.
        prefixos_por_linha = {}
        for onibus in onibus_selecionados:
            prefixos_por_linha.setdefault(onibus.codigo_linha, set()).add(str(onibus.prefixo))

        try:
            cliente = SPTransClient()
            recebidos, erros, linhas_ok = self._consultar_posicoes(
                cliente, prefixos_por_linha, execucao.id, iniciado_em
            )
            novos = self._descartar_duplicados(recebidos)
            if novos:
                db.session.add_all(novos)
            execucao.finalizada_em = datetime.now(timezone.utc)
            execucao.recebidos = len(recebidos)
            execucao.novos_persistidos = len(novos)
            if not erros:
                execucao.estado = "OK"
            elif linhas_ok:
                execucao.estado = "PARCIAL"
            else:
                execucao.estado = "FALHA"
            execucao.erro = "; ".join(erros) or None
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
    def _consultar_posicoes(cliente, prefixos_por_linha, execucao_id, iniciado_em):
        registros = []
        erros = []
        linhas_ok = 0

        for codigo_linha, prefixos in prefixos_por_linha.items():
            try:
                resposta = cliente.buscar_posicoes(codigo_linha)
            except Exception:
                logger.exception("Falha consultando posições da linha %s", codigo_linha)
                erros.append(f"linha {codigo_linha}: falha na consulta")
                continue
            linhas_ok += 1

            # A resposta da API Olho Vivo organiza os veículos no array `vs`.
            veiculos = resposta.get("vs", []) if isinstance(resposta, dict) else []
            for veiculo in veiculos:
                if not isinstance(veiculo, dict):
                    continue
                prefixo = veiculo.get("p")
                prefixo = None if prefixo is None else str(prefixo)
                if prefixo not in prefixos:
                    continue

                # O objeto original vai no payload; as colunas tipadas são uma
                # leitura dele, com NULL para ausentes ou inválidos.
                registros.append(
                    RegistroBrutoGPS(
                        execucao_id=execucao_id,
                        # Selecionados sempre referenciam um Veiculo existente.
                        veiculo_prefixo=prefixo,
                        prefixo_origem=prefixo,
                        linha_origem=str(codigo_linha),
                        latitude=_ler_coordenada(veiculo.get("py"), 90),
                        longitude=_ler_coordenada(veiculo.get("px"), 180),
                        horario_gps=_ler_horario_api(veiculo.get("ta")),
                        coletado_em=iniciado_em,
                        payload=veiculo,
                    )
                )

        return registros, erros, linhas_ok

    @staticmethod
    def _descartar_duplicados(registros):
        """Remove só duplicações identificáveis com segurança.

        - Com `horario_gps`: a mesma posição (prefixo + horario_gps) já gravada em
          qualquer ciclo, ou repetida neste, não é gravada de novo.
        - Sem `horario_gps`: só é descartado o objeto idêntico (mesmo payload)
          repetido dentro deste ciclo. Coordenadas iguais em ciclos diferentes
          não provam duplicação, então registros sem horário de ciclos
          diferentes são todos preservados.
        """
        com_horario = [r for r in registros if r.horario_gps is not None]
        existentes = set()
        if com_horario:
            existentes = set(
                tuple(linha)
                for linha in db.session.query(
                    RegistroBrutoGPS.prefixo_origem, RegistroBrutoGPS.horario_gps
                ).filter(
                    RegistroBrutoGPS.prefixo_origem.in_({r.prefixo_origem for r in com_horario}),
                    RegistroBrutoGPS.horario_gps.in_({r.horario_gps for r in com_horario}),
                ).all()
            )

        payloads_sem_horario = set()
        novos = []
        for registro in registros:
            if registro.horario_gps is not None:
                chave = (registro.prefixo_origem, registro.horario_gps)
                if chave in existentes:
                    continue
                existentes.add(chave)
            else:
                chave = (
                    registro.prefixo_origem,
                    json.dumps(registro.payload, sort_keys=True, default=str),
                )
                if chave in payloads_sem_horario:
                    continue
                payloads_sem_horario.add(chave)
            novos.append(registro)
        return novos


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
