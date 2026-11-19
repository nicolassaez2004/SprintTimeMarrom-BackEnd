"""Cenários de negócio da US04-T01, sem rede nem banco compartilhado."""

import hashlib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy.exc import IntegrityError

from app import db
from app.gtfs.service import VersaoGTFSAmbigua
from app.integrations.programacao import CapturaProgramacao, FonteOficialIndisponivel
from app.models import ArquivoArmazenado, ImportacaoProgramacao, Linha, PartidaProgramada, VersaoGTFS
from app.programacao.service import consultar_oficial, importar_oficial

DATA = date(2026, 11, 19)
AGORA = datetime(2026, 11, 19, 12, tzinfo=timezone.utc)
CONTEXTO = ("3762-10", "ida", DATA, "UTIL")


def test_importa_consulta_com_procedencia_e_data_operacional(versao, fabrica_captura, app):
    captura = fabrica_captura()
    resultado = importar_oficial(*CONTEXTO, captura=captura, agora=AGORA)
    assert resultado["estado"] == "IMPORTADA" and resultado["oficial_disponivel"] is True
    assert resultado["quantidade"] == 3
    assert [p["saida_prevista"] for p in resultado["partidas"]] == [
        "2026-11-19T08:00:00+00:00", "2026-11-20T02:50:00+00:00", "2026-11-20T03:10:00+00:00",
    ]
    item = ImportacaoProgramacao.query.one()
    assert item.arquivo.sha256 == hashlib.sha256(captura.conteudo).hexdigest() == item.versao_fonte
    bruto = Path(app.instance_path) / "programacao" / Path(item.arquivo.chave).name
    assert bruto.read_bytes() == captura.conteudo
    partida = PartidaProgramada.query.order_by(PartidaProgramada.ordem).first()
    assert partida.linha.codigo == "3762-10" and partida.data_operacao == DATA
    assert partida.sentido == "0" and partida.fonte == "OFICIAL"
    assert partida.versao_gtfs.id == versao.id and partida.versao_fonte == item.versao_fonte
    db.session.remove()
    consulta = consultar_oficial(*CONTEXTO)
    assert consulta["estado"] == "ARMAZENADA" and "oficial_disponivel" not in consulta
    assert consulta["procedencia"]["url"] == captura.fonte_url
    assert consulta["procedencia"]["versao_gtfs_id"] == versao.id
    assert consulta["partidas"] == resultado["partidas"]


def test_repeticao_nao_duplica_e_versoes_anteriores_sao_recuperaveis(versao, fabrica_captura):
    a = fabrica_captura(("05:00",))
    b = fabrica_captura(("05:05",))
    r_a = importar_oficial(*CONTEXTO, captura=a, agora=AGORA)
    r_b = importar_oficial(*CONTEXTO, captura=b, agora=AGORA + timedelta(minutes=1))
    assert r_a["procedencia"]["versao_fonte"] != r_b["procedencia"]["versao_fonte"]
    assert consultar_oficial(*CONTEXTO)["partidas"] == r_b["partidas"]
    hash_a = r_a["procedencia"]["versao_fonte"]
    assert consultar_oficial(*CONTEXTO, versao_fonte=hash_a)["partidas"] == r_a["partidas"]
    repetida = importar_oficial(*CONTEXTO, captura=a, agora=AGORA + timedelta(minutes=2))
    assert repetida["estado"] == "REPETIDA"
    assert ImportacaoProgramacao.query.count() == 2 and PartidaProgramada.query.count() == 2
    assert consultar_oficial(*CONTEXTO)["partidas"] == r_a["partidas"]
    assert repetida["procedencia"]["capturada_em"] == AGORA.isoformat()
    assert repetida["procedencia"]["verificada_em"] == (AGORA + timedelta(minutes=2)).isoformat()


def test_sentido_data_e_tipo_dia_nao_misturam_partidas(versao, fabrica_captura):
    captura = fabrica_captura(("05:00",), volta=("06:00",))
    ida = importar_oficial(*CONTEXTO, captura=captura)
    volta = importar_oficial("3762-10", "volta", DATA, "UTIL", captura=captura)
    amanha = importar_oficial("3762-10", "ida", DATA + timedelta(days=1), "SABADO",
                              captura=fabrica_captura(("07:00",), tipo=1))
    assert ida["partidas"][0]["saida_prevista"] == "2026-11-19T08:00:00+00:00"
    assert volta["partidas"][0]["saida_prevista"] == "2026-11-19T09:00:00+00:00"
    assert amanha["partidas"][0]["saida_prevista"] == "2026-11-20T10:00:00+00:00"
    assert ImportacaoProgramacao.query.count() == 3
    assert consultar_oficial("3762-10", "volta", DATA, "SABADO")["estado"] == "NAO_IMPORTADA"


def test_zero_partidas_validas_difere_de_fonte_indisponivel(versao, fabrica_captura):
    resultado = importar_oficial(*CONTEXTO, captura=fabrica_captura(()))
    assert resultado["oficial_disponivel"] is True and resultado["quantidade"] == 0
    assert resultado["partidas"] == [] and "fallback" not in resultado
    assert ImportacaoProgramacao.query.count() == 1
    assert consultar_oficial(*CONTEXTO)["estado"] == "ARMAZENADA"


@pytest.mark.parametrize("motivo", ["HTTP_403", "FALHA_DE_REDE", "LINHA_NAO_PUBLICADA"])
def test_ausencia_da_fonte_encaminha_fallback_sem_fabricar_partidas(versao, motivo, caplog):
    cliente = Mock()
    cliente.obter.side_effect = FonteOficialIndisponivel(motivo)
    resultado = importar_oficial(*CONTEXTO, cliente=cliente)
    assert resultado["estado"] == "FALLBACK_GTFS_NECESSARIO"
    assert resultado["oficial_disponivel"] is False and resultado["motivo"] == motivo
    # A US04-T02 já tenta gerar o fallback. Esta fixture só registra os
    # metadados do GTFS e não fornece um ZIP: deve falhar sem fabricar saídas.
    assert resultado["fallback"]["fonte"] == "GTFS"
    assert resultado["fallback"]["estado"] == "FALHA_GERACAO"
    assert resultado["fallback"]["versao_gtfs_id"] == versao.id
    assert "ZIP GTFS" in resultado["fallback"]["motivo"]
    assert resultado["partidas"] == [] and ImportacaoProgramacao.query.count() == 0
    assert motivo in caplog.text


def test_sem_versao_gtfs_o_fallback_expoe_precondicao_em_vez_de_escolher(app):
    cliente = Mock()
    cliente.obter.side_effect = FonteOficialIndisponivel("HTTP_403")
    resultado = importar_oficial(*CONTEXTO, cliente=cliente)
    assert resultado["fallback"]["estado"] == "SEM_VERSAO_GTFS"


def test_erro_da_fonte_prioriza_oficial_armazenada_sem_alterar_verificacao(versao, fabrica_captura):
    anterior = importar_oficial(*CONTEXTO, captura=fabrica_captura(), agora=AGORA)
    cliente = Mock()
    cliente.obter.side_effect = FonteOficialIndisponivel("HTTP_403")
    resultado = importar_oficial(*CONTEXTO, cliente=cliente)
    assert resultado["estado"] == "OFICIAL_PRIORITARIA"
    assert resultado["oficial_disponivel"] is True
    assert resultado["partidas"] == anterior["partidas"]
    assert resultado["motivo"] == "HTTP_403"
    assert resultado["procedencia"]["verificada_em"] == AGORA.isoformat()
    assert consultar_oficial(*CONTEXTO)["partidas"] == anterior["partidas"]
    assert ImportacaoProgramacao.query.one().verificada_em == AGORA


@pytest.mark.parametrize("mudanca", [
    {"codigo": "0000-00"}, {"partidasIda": None}, {"codigoErro": 1},
    {"partidasIda": [{"tipoDia": 0, "horariosProgramados": [{"horario": "12:90"}]}]},
])
def test_entrada_invalida_nao_persiste_nem_substitui_versao_valida(versao, fabrica_captura, mudanca):
    importar_oficial(*CONTEXTO, captura=fabrica_captura())
    resultado = importar_oficial(*CONTEXTO, captura=fabrica_captura(**mudanca))
    assert resultado["motivo"] == "RESPOSTA_OFICIAL_INVALIDA"
    assert ImportacaoProgramacao.query.count() == 1 and PartidaProgramada.query.count() == 3


def test_falha_sql_desfaz_partidas_e_metadados_e_permite_retomada(versao, fabrica_captura, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(db.session, "commit", Mock(side_effect=RuntimeError("falha simulada")))
        with pytest.raises(RuntimeError, match="falha simulada"):
            importar_oficial(*CONTEXTO, captura=fabrica_captura())
    assert ImportacaoProgramacao.query.count() == 0 and PartidaProgramada.query.count() == 0
    assert ArquivoArmazenado.query.count() == 1  # Só o GTFS da fixture.
    assert importar_oficial(*CONTEXTO, captura=fabrica_captura())["estado"] == "IMPORTADA"


def test_json_alterado_no_filestore_nao_e_devolvido_como_integro(versao, fabrica_captura, app):
    resultado = importar_oficial(*CONTEXTO, captura=fabrica_captura())
    caminho = Path(app.instance_path) / "programacao" / Path(resultado["procedencia"]["arquivo"]).name
    caminho.write_bytes(b"arquivo alterado")
    with pytest.raises(FileNotFoundError, match="hash"):
        consultar_oficial(*CONTEXTO)
    with pytest.raises(RuntimeError, match="divergente"):
        importar_oficial(*CONTEXTO, captura=fabrica_captura())


def test_precondicoes_gtfs_nao_sao_confundidas_com_falha_da_fonte(versao, fabrica_captura):
    linha = db.session.get(Linha, (versao.id, "3762-10"))
    db.session.delete(linha)
    db.session.commit()
    with pytest.raises(LookupError, match="linha não importada"):
        importar_oficial(*CONTEXTO, captura=fabrica_captura())
    versao_b = VersaoGTFS(hash="b" * 64, capturada_em=AGORA, vigencia_inicio=DATA,
                          arquivo=ArquivoArmazenado(chave="gtfs/b.zip", nome_original="b.zip",
                                                     mime_type="application/zip", tamanho_bytes=0,
                                                     sha256="b" * 64, criado_em=AGORA))
    db.session.add(versao_b)
    db.session.commit()
    with pytest.raises(VersaoGTFSAmbigua):
        importar_oficial(*CONTEXTO, captura=fabrica_captura())
    db.session.add(Linha(versao_gtfs_id=versao_b.id, codigo="3762-10", nome="Linha versão B"))
    db.session.commit()
    importada = importar_oficial(*CONTEXTO, captura=fabrica_captura(), versao_gtfs_id=versao_b.id)
    assert importada["procedencia"]["versao_gtfs_id"] == versao_b.id
    assert consultar_oficial(*CONTEXTO, versao_gtfs_id=versao_b.id)["partidas"] == importada["partidas"]
    with pytest.raises(LookupError, match="não cobre"):
        importar_oficial(*CONTEXTO, captura=fabrica_captura(), versao_gtfs_id="inexistente")
    cliente = Mock()
    cliente.obter.side_effect = FonteOficialIndisponivel("HTTP_403")
    assert importar_oficial(*CONTEXTO, cliente=cliente)["fallback"]["estado"] == "VERSAO_GTFS_AMBIGUA"


def test_banco_proibe_partida_de_importacao_inexistente_e_frequencia_sem_intervalo(versao, fabrica_captura):
    for partida in [PartidaProgramada(importacao_id="inexistente", ordem=1, saida_prevista=AGORA),
                    PartidaProgramada(importacao_id="inexistente", ordem=0, saida_prevista=AGORA)]:
        db.session.add(partida)
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()
    importar_oficial(*CONTEXTO, captura=fabrica_captura())
    partida = PartidaProgramada.query.first()
    partida.tipo_oferta = "FREQUENCIA"
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_json_sem_autenticidade_nao_aceita_url_de_outro_dominio(versao, fabrica_captura):
    captura = CapturaProgramacao(fabrica_captura().conteudo, "https://exemplo.com/programacao")
    resultado = importar_oficial(*CONTEXTO, captura=captura)
    assert resultado["oficial_disponivel"] is False
    assert ImportacaoProgramacao.query.count() == 0
