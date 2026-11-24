from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import event

from app import create_app, db
from app.models import (
    ArquivoArmazenado,
    ImportacaoProgramacao,
    Linha,
    PartidaProgramada,
    VersaoGTFS,
)

AGORA = datetime(2026, 11, 24, 12, tzinfo=timezone.utc)


@pytest.fixture
def app(tmp_path):
    application = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite://",
        "MYSQL_SSL_CA": None,
    })
    application.instance_path = str(tmp_path / "instance")
    with application.app_context():
        event.listen(db.engine, "connect", lambda conexao, _: conexao.execute("PRAGMA foreign_keys=ON"))
        db.create_all()
        yield application
        db.session.remove()
        db.engine.dispose()


@pytest.fixture
def programacao(app):
    arquivo = ArquivoArmazenado(
        chave="gtfs/teste.zip", nome_original="teste.zip", mime_type="application/zip",
        tamanho_bytes=0, sha256="a" * 64, criado_em=AGORA,
    )
    versao = VersaoGTFS(
        hash="a" * 64, capturada_em=AGORA, vigencia_inicio=date(2026, 1, 1),
        vigencia_fim=date(2026, 12, 31), arquivo=arquivo,
    )
    linha = Linha(versao_gtfs=versao, codigo="3762-10", nome="Linha sintética")
    db.session.add_all([versao, linha])
    db.session.flush()
    importacao = ImportacaoProgramacao(
        versao_gtfs_id=versao.id, linha=linha, data_operacao=date(2026, 11, 24),
        sentido="0", tipo_dia="UTIL", fonte="OFICIAL", versao_fonte="captura-1",
        fonte_url="https://example.invalid/grade", capturada_em=AGORA,
        verificada_em=AGORA, arquivo=arquivo,
    )
    db.session.add(importacao)
    db.session.flush()
    partidas = []
    for indice, minuto in enumerate((0, 15, 30), start=1):
        item = PartidaProgramada(
            importacao=importacao, ordem=indice,
            saida_prevista=AGORA + timedelta(minutes=minuto),
            tipo_oferta="FIXA", intervalo_seg=None,
        )
        db.session.add(item)
        partidas.append(item)
    db.session.commit()
    return versao, linha, importacao, partidas
