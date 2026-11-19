"""Banco local e respostas SINTÉTICAS; não são capturas reais da SPTrans."""

import json
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import event

from app import create_app, db
from app.integrations.programacao import BASE_OFICIAL, CapturaProgramacao
from app.models import ArquivoArmazenado, Linha, VersaoGTFS

DATA = date(2026, 11, 19)
AGORA = datetime(2026, 11, 19, 12, tzinfo=timezone.utc)


@pytest.fixture
def app(tmp_path):
    aplicacao = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://",
                            "MYSQL_SSL_CA": None, "PROGRAMACAO_STORAGE_DIR": None})
    aplicacao.instance_path = str(tmp_path / "instance")
    with aplicacao.app_context():
        event.listen(db.engine, "connect", lambda conexao, _: conexao.execute("PRAGMA foreign_keys=ON"))
        db.create_all()
        yield aplicacao
        db.session.remove()
        db.engine.dispose()


@pytest.fixture
def versao(app):
    arquivo = ArquivoArmazenado(chave="gtfs/sintetico.zip", nome_original="sintetico.zip",
                                mime_type="application/zip", tamanho_bytes=0,
                                sha256="a" * 64, criado_em=AGORA)
    versao = VersaoGTFS(hash="a" * 64, capturada_em=AGORA, vigencia_inicio=date(2026, 1, 1),
                       vigencia_fim=date(2026, 12, 31), arquivo=arquivo)
    db.session.add(versao)
    db.session.flush()
    db.session.add(Linha(versao_gtfs_id=versao.id, codigo="3762-10", nome="Linha sintética"))
    db.session.commit()
    return versao


@pytest.fixture
def fabrica_captura():
    def criar(horarios=("05:00", "23:50", "00:10"), *, volta=("06:00",), tipo=0, **extras):
        payload = {
            "codigo": "3762-10", "codigoErro": 0,
            "partidasIda": [{"tipoDia": tipo, "horariosProgramados": [{"horario": h} for h in horarios]}],
            "partidasVolta": [{"tipoDia": tipo, "horariosProgramados": [{"horario": h} for h in volta]}],
        }
        payload.update(extras)
        return CapturaProgramacao(json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                  BASE_OFICIAL + "BuscarDetalheLinhaTeste")
    return criar
