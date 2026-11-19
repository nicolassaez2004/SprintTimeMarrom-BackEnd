"""Importação oficial e encaminhamento explícito para o fallback GTFS."""

import hashlib
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from flask import current_app

from app.gtfs.service import VersaoGTFSAmbigua, VersaoGTFSInexistente, versao_vigente
from app.integrations.programacao import (
    FonteOficialIndisponivel, ProgramacaoOficialInvalida, SPTransProgrammingClient,
    validar_url_oficial,
)
from app.programacao.normalizacao import normalizar, validar_contexto
from app.repositories import programacao as repositorio
from app.storage import FileStore


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


def preparar_fallback_gtfs(linha, sentido, data_operacao, tipo_dia, motivo, versao_gtfs_id=None):
    """Resolve os pré-requisitos da geração GTFS, sem inventar partidas.

    O gerador de oferta GTFS pertence à continuação da US04. Este contrato
    permite ao chamador encaminhar o contexto validado àquela implementação.
    """
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
            "url": item.fonte_url, "capturada_em": item.capturada_em.isoformat(),
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
        return preparar_fallback_gtfs(codigo, sentido, data_operacao, tipo_dia, motivo, versao_gtfs_id)

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
