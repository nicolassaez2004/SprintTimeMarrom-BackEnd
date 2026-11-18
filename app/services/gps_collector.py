"""Coleta periódica das posições dos ônibus selecionados."""

import logging
import argparse
import time
from datetime import datetime, timezone

from app import create_app, db
from app.models import OnibusSelecionado, RegistroGPS
from app.services.sptrans import SPTransClient


logger = logging.getLogger(__name__)
INTERVALO_COLETA_SEGUNDOS = 30


def coletar_ciclo():
    """Consulta as linhas selecionadas e persiste posições dos prefixos salvos."""
    onibus_selecionados = OnibusSelecionado.query.all()
    if not onibus_selecionados:
        logger.info("Nenhum ônibus selecionado; ciclo sem consultas.")
        return 0

    # A API é consultada uma vez por linha, mesmo que vários ônibus selecionados
    # pertençam à mesma linha.
    prefixos_por_linha = {}
    for onibus in onibus_selecionados:
        prefixos_por_linha.setdefault(onibus.codigo_linha, set()).add(onibus.prefixo)

    cliente = SPTransClient()
    registros = []
    consultado_em = datetime.now(timezone.utc)

    for codigo_linha, prefixos in prefixos_por_linha.items():
        try:
            resposta = cliente.buscar_posicoes(codigo_linha)
        except Exception:
            logger.exception("Falha consultando posições da linha %s", codigo_linha)
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
                    prefixo_onibus=prefixo,
                    codigo_linha=codigo_linha,
                    latitude=latitude,
                    longitude=longitude,
                    registrado_em=consultado_em,
                )
            )

    if registros:
        db.session.add_all(registros)
        db.session.commit()
    logger.info("Ciclo concluído: %d posições gravadas.", len(registros))
    return len(registros)


def executar_coletor(intervalo=INTERVALO_COLETA_SEGUNDOS):
    """Executa ciclos com início espaçado pelo intervalo configurado."""
    proxima_execucao = time.monotonic()
    while True:
        espera = proxima_execucao - time.monotonic()
        if espera > 0:
            time.sleep(espera)

        try:
            coletar_ciclo()
        except Exception:
            db.session.rollback()
            logger.exception("Falha no ciclo de coleta GPS")
        finally:
            db.session.remove()

        proxima_execucao += intervalo
        # Em caso de consulta mais lenta que o intervalo, não inicia ciclos
        # simultâneos nem tenta recuperar ciclos perdidos em sequência.
        if proxima_execucao <= time.monotonic():
            proxima_execucao = time.monotonic() + intervalo


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
            executar_coletor()


if __name__ == "__main__":
    main()
