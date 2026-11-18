"""Opt-in: somente base MySQL vazia e descartável us12_verificacao_*.

O provisionamento dessa base e sua remoção pertencem ao executor do teste.
O teste nunca usa DATABASE_URL/TEST_DATABASE_URL das bases compartilhadas.
"""
import json
import os
from pathlib import Path

import pytest
from flask_migrate import upgrade
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from app import create_app, db


@pytest.mark.parametrize('historico_previo', [False, True])
def test_cadeia_mysql_e_integridade(historico_previo):
    key = 'US12_MYSQL_TEST_URL_HISTORICO' if historico_previo else 'US12_MYSQL_TEST_URL_VAZIO'
    value = os.getenv(key)
    if not value:
        pytest.skip(f'Configure {key} para integração MySQL isolada')
    url = make_url(value)
    assert url.get_backend_name() == 'mysql'
    assert url.database.startswith('us12_verificacao_'), 'Recusa base de uso do time'
    app = create_app({'TESTING': True, 'SQLALCHEMY_DATABASE_URI': url, 'MYSQL_SSL_CA': None})
    migrations = str(Path(__file__).resolve().parents[1] / 'migrations')
    revisao_atual = ScriptDirectory(migrations).get_current_head()
    with app.app_context():
        try:
            assert not inspect(db.engine).get_table_names(), 'Exige base vazia'
            if historico_previo:
                upgrade(directory=migrations, revision='0003')
                with db.engine.begin() as conn:
                    conn.execute(text("INSERT INTO execucoes_coleta "
                        "(id,iniciada_em,estado,novos_persistidos) VALUES (1,UTC_TIMESTAMP(6),'OK',1)"))
                    conn.execute(text("INSERT INTO registros_gps "
                        "(id,execucao_id,coletado_em,payload) VALUES (1,1,UTC_TIMESTAMP(6),:payload)"),
                        {'payload': json.dumps({'p': '5810', 'py': 'invalida', 'extra': [1, None]})})
            upgrade(directory=migrations)
            schema = inspect(db.engine)
            assert 'observacoes_tratadas' in schema.get_table_names()
            fks = {f['name']: f for f in schema.get_foreign_keys('observacoes_tratadas')}
            assert fks['fk_observacoes_tratadas_bruto']['referred_table'] == 'registros_gps'
            assert fks['fk_observacoes_tratadas_gtfs']['referred_table'] == 'versoes_gtfs'
            assert {'bruto_id'} in [set(c['column_names']) for c in schema.get_unique_constraints('observacoes_tratadas')]
            with db.engine.connect() as conn:
                engine, collation = conn.execute(text("SELECT ENGINE,TABLE_COLLATION "
                    "FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() "
                    "AND TABLE_NAME='observacoes_tratadas'")).one()
                assert engine == 'InnoDB' and collation.startswith('utf8mb4_')
                assert conn.execute(text('SELECT version_num FROM alembic_version')).scalar_one() == revisao_atual
            if historico_previo:
                with db.engine.begin() as conn:
                    conn.execute(text("INSERT INTO observacoes_tratadas "
                        "(id,bruto_id,valida,motivo_invalidez,versao_correspondencia) "
                        "VALUES ('tratado-1',1,0,'coordenada inválida','v1')"))
                with db.engine.connect() as conn:
                    saved = conn.execute(text('SELECT payload FROM registros_gps WHERE id=1')).scalar_one()
                    assert json.loads(saved) == {'p': '5810', 'py': 'invalida', 'extra': [1, None]}
                sql_invalidos = [
                    "INSERT INTO observacoes_tratadas (id,bruto_id,valida,versao_correspondencia) VALUES ('duplicado',1,1,'v1')",
                    "INSERT INTO observacoes_tratadas (id,bruto_id,valida,versao_correspondencia) VALUES ('orfao',999,1,'v1')",
                    "DELETE FROM registros_gps WHERE id=1",
                    "UPDATE observacoes_tratadas SET versao_gtfs_id='inexistente' WHERE id='tratado-1'",
                    "UPDATE observacoes_tratadas SET versao_correspondencia='' WHERE id='tratado-1'",
                ]
                for sql, codigo in zip(sql_invalidos, [1062, 1452, 1451, 1452, 3819]):
                    with pytest.raises(DBAPIError) as erro:
                        with db.engine.begin() as conn:
                            conn.execute(text(sql))
                    assert erro.value.orig.args[0] == codigo
                with db.engine.begin() as conn:
                    conn.execute(text("UPDATE observacoes_tratadas SET valida=1, motivo_invalidez=NULL WHERE id='tratado-1'"))
                with db.engine.connect() as conn:
                    saved = conn.execute(text('SELECT payload FROM registros_gps WHERE id=1')).scalar_one()
                    assert json.loads(saved)['py'] == 'invalida'
        finally:
            db.session.remove()
            db.engine.dispose()
