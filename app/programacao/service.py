"""Importação oficial e encaminhamento explícito para o fallback GTFS."""

import hashlib
import os
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

from flask import current_app

from app.gtfs.service import GTFSValidationError, VersaoGTFSAmbigua, VersaoGTFSInexistente, versao_vigente
from app.integrations.programacao import (
    FonteOficialIndisponivel, ProgramacaoOficialInvalida, SPTransProgrammingClient,
    validar_url_oficial,
)
from app.programacao.gtfs import gerar_partidas
from app.programacao.normalizacao import (
    ProgramacaoNormalizada, SENTIDOS, normalizar, validar_contexto,
)
from app.repositories import programacao as repositorio
from app.storage import FileStore


class LinhaNaoEncontrada(LookupError):
    pass


class ProgramacaoAmbigua(LookupError):
    pass


def _armazenamento(diretorio=None):
    return FileStore(diretorio or current_app.config.get("PROGRAMACAO_STORAGE_DIR")
                     or Path(current_app.instance_path) / "programacao")


def _selecionar_versao(data_operacao, versao_gtfs_id=None):
    if versao_gtfs_id is None:
        return versao_vigente(data_operacao)
    versao = repositorio.obter_versao(versao_gtfs_id)
    if versao is None or data_operacao < versao.vigencia_inicio or (
        versao.vigencia_fim is not None and data_operacao > versao.vigencia_fim
    ):
        raise VersaoGTFSInexistente("versão GTFS informada não cobre a data operacional")
    return versao


def preparar_fallback_gtfs(linha, sentido, data_operacao, tipo_dia, motivo,
                             versao_gtfs_id=None, *, diretorio=None, agora=None):
    """Gera a oferta GTFS como fallback somente quando a fonte oficial falha."""
    resultado = {
        "estado": "FALLBACK_GTFS_NECESSARIO", "oficial_disponivel": False,
        "motivo": motivo, "linha": linha, "sentido": sentido,
        "data_operacao": data_operacao.isoformat(), "tipo_dia": tipo_dia,
        "fallback": {"fonte": "GTFS", "estado": "PENDENTE_GERACAO", "versao_gtfs_id": None},
        "partidas": [],
    }
    try:
        versao = _selecionar_versao(data_operacao, versao_gtfs_id)
    except VersaoGTFSInexistente:
        resultado["fallback"]["estado"] = "SEM_VERSAO_GTFS"
    except VersaoGTFSAmbigua:
        resultado["fallback"]["estado"] = "VERSAO_GTFS_AMBIGUA"
    else:
        resultado["fallback"]["versao_gtfs_id"] = versao.id
        if repositorio.obter_linha(versao.id, linha) is None:
            resultado["fallback"]["estado"] = "LINHA_NAO_IMPORTADA"
        else:
            try:
                fallback = gerar_programacao_gtfs(
                    linha, sentido, data_operacao, tipo_dia,
                    versao_gtfs_id=versao.id, diretorio=diretorio, agora=agora,
                )
            except (GTFSValidationError, LookupError, OSError) as erro:
                resultado["fallback"]["estado"] = "FALHA_GERACAO"
                resultado["fallback"]["motivo"] = str(erro)
            else:
                if fallback.get("oficial_disponivel"):
                    fallback["motivo"] = motivo
                    return fallback
                fallback["estado"] = "FALLBACK_GTFS"
                fallback["oficial_disponivel"] = False
                fallback["motivo"] = motivo
                fallback["fallback"] = {
                    "fonte": "GTFS", "estado": "GERADO", "versao_gtfs_id": versao.id,
                }
                return fallback
    current_app.logger.warning("programacao_oficial_indisponivel motivo=%s fallback=%s",
                               motivo, resultado["fallback"]["estado"])
    return resultado


def _serializar(item, estado):
    return {
        "estado": estado, "linha": item.linha_codigo, "sentido": item.sentido,
        "data_operacao": item.data_operacao.isoformat(), "tipo_dia": item.tipo_dia,
        "quantidade": len(item.partidas),
        "procedencia": {
            "fonte": item.fonte, "versao_fonte": item.versao_fonte,
            "url": item.fonte_url if item.fonte == "OFICIAL" else None,
            "referencia": item.fonte_url if item.fonte == "GTFS" else None,
            "capturada_em": item.capturada_em.isoformat(),
            "verificada_em": item.verificada_em.isoformat(), "arquivo": item.arquivo.chave,
            "versao_gtfs_id": item.versao_gtfs_id,
        },
        "partidas": [{"id": p.id, "ordem": p.ordem,
                      "saida_prevista": p.saida_prevista.isoformat(),
                      "tipo_oferta": p.tipo_oferta, "intervalo_seg": p.intervalo_seg}
                     for p in item.partidas],
    }


def importar_oficial(linha, sentido, data_operacao, tipo_dia, *, cliente=None,
                     captura=None, diretorio=None, agora=None, versao_gtfs_id=None):
    """Consulta explícita; não é executada na inicialização da API.

    `tipo_dia` deve ser decidido pelo calendário operacional do chamador.
    `captura` aceita um JSON oficial obtido externamente para importação manual;
    o hash comprova integridade do arquivo, não a autenticidade de sua origem.
    """
    codigo, sentido, data_operacao, tipo_dia = validar_contexto(
        linha, sentido, data_operacao, tipo_dia,
    )
    if captura is not None and cliente is not None:
        raise ValueError("informe captura manual ou cliente, não ambos")
    if agora is not None and (agora.tzinfo is None or agora.utcoffset() is None):
        raise ValueError("instante de captura deve conter fuso horário")
    try:
        captura = captura if captura is not None else (cliente or SPTransProgrammingClient()).obter(codigo)
        validar_url_oficial(captura.fonte_url)
        programacao = normalizar(captura, codigo, sentido, data_operacao, tipo_dia)
    except (FonteOficialIndisponivel, ProgramacaoOficialInvalida) as erro:
        motivo = erro.motivo if isinstance(erro, FonteOficialIndisponivel) else "RESPOSTA_OFICIAL_INVALIDA"
        return preparar_fallback_gtfs(
            codigo, sentido, data_operacao, tipo_dia, motivo, versao_gtfs_id,
            diretorio=diretorio, agora=agora,
        )

    agora = agora or datetime.now(timezone.utc)

    # A rede é versionada na US02. Falha na pré-condição não transforma uma
    # resposta oficial válida em falha da fonte nem escolhe versão arbitrária.
    versao = _selecionar_versao(data_operacao, versao_gtfs_id)
    if repositorio.obter_linha(versao.id, codigo) is None:
        raise LookupError("linha não importada na versão GTFS vigente; executar a US02 antes")
    digest = hashlib.sha256(captura.conteudo).hexdigest()
    armazenamento = _armazenamento(diretorio)
    fd, temporario = tempfile.mkstemp(prefix="programacao-", suffix=".part", dir=armazenamento.diretorio)
    try:
        with os.fdopen(fd, "wb") as arquivo:
            arquivo.write(captura.conteudo)
            arquivo.flush()
            os.fsync(arquivo.fileno())
        chave = armazenamento.publicar_programacao(temporario, digest, len(captura.conteudo))
        item, nova = repositorio.guardar(programacao, versao.id, digest, chave,
                                        captura.fonte_url, len(captura.conteudo), agora)
    finally:
        Path(temporario).unlink(missing_ok=True)
    resultado = _serializar(item, "IMPORTADA" if nova else "REPETIDA")
    resultado["oficial_disponivel"] = True
    return resultado


def gerar_programacao_gtfs(linha, sentido, data_operacao, tipo_dia, *,
                           versao_gtfs_id=None, diretorio=None, agora=None):
    """Aplica calendário e exceções da versão escolhida e persiste as partidas.

    `linha` é o `route_id` da tabela Linha, e `tipo_dia` é a classificação
    operacional registrada junto da captura. A seleção dos serviços é feita
    exclusivamente por calendar.txt/calendar_dates.txt para a data informada.
    """
    codigo = str(linha).strip() if isinstance(linha, str) else ""
    if not codigo or len(codigo) > 64:
        raise ValueError("linha deve ser um route_id GTFS não vazio com até 64 caracteres")
    sentido_normalizado = SENTIDOS.get(str(sentido).strip().lower())
    if sentido_normalizado is None:
        raise ValueError("sentido GTFS deve ser ida/0 ou volta/1")
    if isinstance(data_operacao, datetime) or not isinstance(data_operacao, date):
        raise TypeError("data_operacao deve ser date, sem inferir a data de um instante")
    tipo = str(tipo_dia).strip().upper()
    if tipo not in ("UTIL", "SABADO", "DOMINGO", "FERIADO"):
        raise ValueError("tipo_dia deve ser UTIL, SABADO, DOMINGO ou FERIADO")
    if agora is not None and (agora.tzinfo is None or agora.utcoffset() is None):
        raise ValueError("instante de geração deve conter fuso horário")

    versao_selecionada = _selecionar_versao(data_operacao, versao_gtfs_id)
    oficial = repositorio.consultar(
        versao_selecionada.id, codigo, sentido_normalizado, data_operacao, tipo,
        fonte="OFICIAL",
    )
    if oficial is not None:
        resultado = _serializar(oficial, "OFICIAL_PRIORITARIA")
        resultado["oficial_disponivel"] = True
        return resultado

    versao, saidas = gerar_partidas(
        versao_selecionada.id, codigo, sentido_normalizado, data_operacao,
        diretorio=diretorio,
    )
    if repositorio.obter_linha(versao.id, codigo) is None:
        raise LookupError("linha não importada nesta versão GTFS; executar a US02 antes")

    agora = agora or datetime.now(timezone.utc)
    programacao = ProgramacaoNormalizada(
        codigo, sentido_normalizado, data_operacao, tipo, saidas,
    )
    digest = versao.hash
    item, nova = repositorio.guardar(
        programacao, versao.id, digest, versao.arquivo.chave,
        f"urn:sha256:{digest}", versao.arquivo.tamanho_bytes, agora,
        fonte="GTFS", arquivo_existente=versao.arquivo,
        capturada_em=versao.capturada_em,
    )
    resultado = _serializar(item, "IMPORTADA" if nova else "REPETIDA")
    resultado["servico_gtfs"] = {"versao_gtfs_id": versao.id, "hash": digest}
    return resultado


def consultar_oficial(linha, sentido, data_operacao, tipo_dia, *, versao_fonte=None,
                      diretorio=None, versao_gtfs_id=None):
    """Recupera captura armazenada; não afirma disponibilidade atual da fonte."""
    codigo, sentido, data_operacao, tipo_dia = validar_contexto(linha, sentido, data_operacao, tipo_dia)
    versao = _selecionar_versao(data_operacao, versao_gtfs_id)
    item = repositorio.consultar(versao.id, codigo, sentido, data_operacao, tipo_dia, versao_fonte)
    if item is None:
        return {"estado": "NAO_IMPORTADA", "partidas": [], "procedencia": None}
    # Nunca devolver como verificável uma captura cujo bruto sumiu ou mudou.
    _armazenamento(diretorio).caminho_programacao(item.arquivo.chave, item.versao_fonte)
    return _serializar(item, "ARMAZENADA")


def consultar_partidas(filtros):
    """Leitura para HTTP: nenhum download, geração, gravação ou acesso ao FileStore.

    Sem tipo de dia explícito, só atende um contexto de calendário inequívoco.
    A fonte oficial tem prioridade, conforme a US04-T02B. Dentro da fonte,
    escolhe a captura verificada mais recentemente.
    """
    versao = _selecionar_versao(filtros.data, filtros.versao_gtfs_id)
    if repositorio.obter_linha(versao.id, filtros.linha) is None:
        raise LinhaNaoEncontrada("Linha não encontrada na versão GTFS selecionada.")
    item, tipos = repositorio.consultar_por_dia(
        versao.id, filtros.linha, filtros.sentido, filtros.data, filtros.tipo_dia,
    )
    if len(tipos) > 1:
        raise ProgramacaoAmbigua("Há grades de tipos de dia diferentes; informe tipo_dia.")
    if item is None:
        return {"estado": "NAO_IMPORTADA", "linha": filtros.linha, "sentido": filtros.sentido,
                "data_operacao": filtros.data.isoformat(), "tipo_dia": filtros.tipo_dia,
                "quantidade": 0, "procedencia": None, "partidas": []}
    return _serializar(item, "DISPONIVEL" if item.partidas else "SEM_PARTIDAS")
