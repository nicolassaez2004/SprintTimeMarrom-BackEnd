"""Coleta periódica das posições dos ônibus selecionados."""

import logging
import argparse
import time
from datetime import datetime, timezone

from sqlalchemy import text

from app import create_app, db
from app.config import validar_intervalo_coleta
from app.models import ExecucaoColeta, OnibusSelecionado, RegistroGPS
from app.services.sptrans import SPTransClient


logger = logging.getLogger(__name__)
INTERVALO_COLETA_SEGUNDOS = 30
CHAVE_LOCK_COLETOR = 728194521


def coletar_ciclo():
    """Consulta as linhas selecionadas e persiste posições dos prefixos salvos."""
    iniciado_em = datetime.now(timezone.utc)
    execucao = ExecucaoColeta(iniciada_em=iniciado_em, status="executando")
    db.session.add(execucao)
    db.session.flush()

    onibus_selecionados = OnibusSelecionado.query.all()
    if not onibus_selecionados:
        execucao.finalizada_em = datetime.now(timezone.utc)
        execucao.status = "concluida"
        execucao.quantidade_registros = 0
        db.session.commit()
        logger.info("Nenhum ônibus selecionado; ciclo sem consultas.")
        return 0

    # A API é consultada uma vez por linha, mesmo que vários ônibus selecionados
    # pertençam à mesma linha.
    prefixos_por_linha = {}
    for onibus in onibus_selecionados:
        prefixos_por_linha.setdefault(onibus.codigo_linha, set()).add(onibus.prefixo)

    try:
        cliente = SPTransClient()
    except Exception as error:
        execucao.finalizada_em = datetime.now(timezone.utc)
        execucao.status = "falha"
        execucao.erro = type(error).__name__
        db.session.commit()
        logger.exception("Não foi possível iniciar o cliente SPTrans")
        return 0

    registros = []
    erros = []

    for codigo_linha, prefixos in prefixos_por_linha.items():
        try:
            resposta = cliente.buscar_posicoes(codigo_linha)
        except Exception:
            logger.exception("Falha consultando posições da linha %s", codigo_linha)
            erros.append(f"linha {codigo_linha}: falha na consulta")
            continue

        # A resposta da API Olho Vivo organiza os veículos no array `vs`.
        veiculos = resposta.get("vs", []) if isinstance(resposta, dict) else []
        for veiculo in veiculos:
            prefixo = veiculo.get("p")
            latitude = veiculo.get("py")
            longitude = veiculo.get("px")
            if prefixo not in prefixos or latitude is None or longitude is None:
                continue

            registros.append(
                RegistroGPS(
                    execucao_id=execucao.id,
                    prefixo_onibus=prefixo,
                    codigo_linha=codigo_linha,
                    latitude=latitude,
                    longitude=longitude,
                    registrado_em=iniciado_em,
                )
            )

    if registros:
        db.session.add_all(registros)
    execucao.finalizada_em = datetime.now(timezone.utc)
    execucao.quantidade_registros = len(registros)
    execucao.status = "parcial" if erros and registros else "falha" if erros else "concluida"
    execucao.erro = "; ".join(erros) or None
    db.session.commit()
    logger.info("Ciclo concluído: %d posições gravadas.", len(registros))
    return len(registros)


def executar_coletor(
    intervalo_segundos=None,
    *,
    coletar=None,
    relogio=None,
    dormir=None,
    limpar_sessao=None,
    max_ciclos=None,
):
    """Roda ciclos em cadência fixa; relógio/espera são injetáveis para testes."""
    intervalo = validar_intervalo_coleta(
        INTERVALO_COLETA_SEGUNDOS if intervalo_segundos is None else intervalo_segundos
    )
    coletar = coletar or coletar_ciclo
    relogio = relogio or time.monotonic
    dormir = dormir or time.sleep
    limpar_sessao = limpar_sessao or db.session.remove

    proxima_execucao = relogio()
    ciclos = 0
    while max_ciclos is None or ciclos < max_ciclos:
        espera = proxima_execucao - relogio()
        if espera > 0:
            dormir(espera)

        try:
            coletar()
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


def _adquirir_lock_coletor():
    """Garante um único processo coletor por banco PostgreSQL."""
    if db.engine.dialect.name != "postgresql":
        raise RuntimeError("O processo coletor requer PostgreSQL para o lock singleton")
    conexao = db.engine.connect()
    try:
        lock_adquirido = conexao.execute(
            text("SELECT pg_try_advisory_lock(:chave)"),
            {"chave": CHAVE_LOCK_COLETOR},
        ).scalar_one()
    except Exception:
        conexao.close()
        raise
    if not lock_adquirido:
        conexao.close()
        return None
    return conexao


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
            lock = _adquirir_lock_coletor()
            if lock is None:
                logger.error("Já existe um processo coletor ativo para este banco.")
                return 1
            try:
                executar_coletor(
                    app.config.get(
                        "GPS_COLLECTION_INTERVAL_SECONDS",
                        INTERVALO_COLETA_SEGUNDOS,
                    )
                )
            finally:
                try:
                    lock.execute(
                        text("SELECT pg_advisory_unlock(:chave)"),
                        {"chave": CHAVE_LOCK_COLETOR},
                    )
                finally:
                    lock.close()
                    db.session.remove()
    return 0


if __name__ == "__main__":
    main()
