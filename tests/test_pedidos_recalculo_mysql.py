"""Opt-in (US20-T02A): somente base MySQL vazia e descartável us20_verificacao_*.

Aplica a cadeia Alembic inteira e dispara pedidos concorrentes com a mesma
chave em threads/conexões distintas. Nunca usa DATABASE_URL das bases do time.
"""
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier

import pytest
from flask_migrate import upgrade
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from app import create_app, db
from app.models import ExecucaoProcessamento
from app.reprocessamento.pedidos import TIPO, registrar_pedidos

INICIO = datetime(2026, 11, 24, 12, tzinfo=timezone.utc)


def _janela(n):
    inicio = INICIO + timedelta(minutes=15 * n)
    return {"id": f"r{n}", "indicador": "PONTUALIDADE_VIAGENS", "inicio": inicio,
            "fim": inicio + timedelta(minutes=15), "linha": "3762-10", "sentido": "0"}


def test_pedidos_concorrentes_no_mysql_nao_duplicam():
    valor = os.getenv("US20_MYSQL_TEST_URL")
    if not valor:
        pytest.skip("Configure US20_MYSQL_TEST_URL para integração MySQL isolada")
    url = make_url(valor)
    assert url.get_backend_name() == "mysql"
    assert url.database.startswith("us20_verificacao_"), "Recusa base de uso do time"
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": url, "MYSQL_SSL_CA": None})
    migrations = str(Path(__file__).resolve().parents[1] / "migrations")
    with app.app_context():
        try:
            assert not inspect(db.engine).get_table_names(), "Exige base vazia"
            upgrade(directory=migrations)
            barreira, threads = Barrier(8), 8

            def pedir(n):
                with app.app_context():
                    barreira.wait()
                    itens = [_janela(0), _janela(1 + n % 2)]  # chave comum + chaves parciais
                    ids = [p.id for p in registrar_pedidos(itens, versao_base="b1",
                           versao_regra="r1", versao_codigo="g1", origem={"thread": n})]
                    db.session.remove()
                    return ids

            with ThreadPoolExecutor(threads) as executor:
                resultados = list(executor.map(pedir, range(threads)))
            assert len({ids[0] for ids in resultados}) == 1
            assert ExecucaoProcessamento.query.filter_by(tipo=TIPO).count() == 3
            with db.engine.connect() as conexao:
                motor = conexao.execute(text(
                    "SELECT ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE()"
                    " AND TABLE_NAME='execucoes_processamento'")).scalar_one()
                assert motor == "InnoDB"
                chave = conexao.execute(text(
                    "SELECT chave_logica FROM execucoes_processamento LIMIT 1")).scalar_one()
            with pytest.raises(DBAPIError) as erro:
                with db.engine.begin() as conexao:
                    conexao.execute(text(
                        "INSERT INTO execucoes_processamento (id,tipo,chave_logica,parametros,estado,"
                        "periodo_inicio,periodo_fim,bruto_id_maximo,versao_base,versao_codigo,"
                        "versao_regra,tentativas,criada_em) SELECT 'dup',tipo,chave_logica,parametros,"
                        "estado,periodo_inicio,periodo_fim,0,'b1','g1','r1',0,criada_em "
                        "FROM execucoes_processamento WHERE chave_logica=:c"), {"c": chave})
            assert erro.value.orig.args[0] == 1062
        finally:
            db.session.remove()
            db.engine.dispose()
