"""Verificação real, opt-in, das duas bases pela factory Flask existente.

Uso: python -m scripts.verificar_bases_mysql
Não executa migrações nem cria tabelas permanentes. Não imprime URLs/senhas.
"""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError


def verificar():
    load_dotenv(Path(__file__).resolve().parents[1] / '.env', override=False)
    urls = []
    for key in ('DATABASE_URL', 'TEST_DATABASE_URL'):
        value = os.getenv(key)
        if not value:
            raise ValueError(f'Preencha {key} no .env local.')
        url = make_url(value)
        if url.drivername != 'mysql+pymysql' or not url.database:
            raise ValueError(f'{key} deve usar mysql+pymysql e identificar a base.')
        if url.query.get('charset') != 'utf8mb4':
            raise ValueError(f'{key} deve incluir ?charset=utf8mb4.')
        urls.append(url)
    # Esta preparação usa duas bases no mesmo servidor, com nomes distintos.
    if urls[0].database == urls[1].database:
        raise ValueError('Desenvolvimento e teste precisam ter nomes diferentes.')
    if (urls[0].host, urls[0].port or 3306) != (urls[1].host, urls[1].port or 3306):
        raise ValueError('Use o mesmo servidor/porta para as duas bases desta preparação.')

    from app import create_app, db

    for label, url in zip(('DESENVOLVIMENTO', 'TESTE'), urls):
        app = create_app({'SQLALCHEMY_DATABASE_URI': url, 'TESTING': True})
        with app.app_context():
            try:
                with db.engine.connect() as conn:
                    info = conn.execute(text(
                        'SELECT VERSION(), DATABASE(), @@default_storage_engine'
                    )).one()
                    tls = conn.execute(text("SHOW SESSION STATUS LIKE 'Ssl_cipher'")).one()[1]
                    if app.config.get('MYSQL_SSL_CA') and not tls:
                        raise ValueError('Servidor remoto sem TLS; conexão recusada.')
                    charset = conn.execute(text(
                        'SELECT DEFAULT_CHARACTER_SET_NAME FROM information_schema.SCHEMATA '
                        'WHERE SCHEMA_NAME = DATABASE()'
                    )).scalar_one()
                    if info[1] != url.database or charset != 'utf8mb4':
                        raise ValueError(f'{label}: base ou charset diferente do esperado.')
                    if info[2].upper() != 'INNODB':
                        raise ValueError('Configure default_storage_engine=InnoDB no servidor antes de migrar.')
                    incompatible = conn.execute(text(
                        "SELECT COUNT(*) FROM information_schema.TABLES "
                        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_TYPE = 'BASE TABLE' "
                        "AND ENGINE <> 'InnoDB'"
                    )).scalar_one()
                    if incompatible:
                        raise ValueError(f'{label}: existem tabelas fora do InnoDB; revisar antes de continuar.')
                    # Tabela temporária é privada desta conexão e some ao fechar.
                    conn.execute(text('CREATE TEMPORARY TABLE us12_t01a_probe '
                                      '(id INTEGER PRIMARY KEY, valor VARCHAR(100)) '
                                      'ENGINE=InnoDB DEFAULT CHARSET=utf8mb4'))
                    try:
                        sample = 'Ônibus São Paulo 🚌'
                        conn.execute(text('INSERT INTO us12_t01a_probe VALUES (1, :v)'), {'v': sample})
                        assert conn.execute(text('SELECT valor FROM us12_t01a_probe')).scalar_one() == sample
                    finally:
                        conn.execute(text('DROP TEMPORARY TABLE us12_t01a_probe'))
                    print(f'{label}: OK | base={info[1]} | MySQL Server={info[0]} | InnoDB | utf8mb4'
                          + (f' | TLS={tls}' if tls else ' | conexão local sem TLS'))
            finally:
                db.engine.dispose()
    print('Duas bases verificadas pela aplicação. Anexar também evidência do Workbench.')


if __name__ == '__main__':
    try:
        verificar()
    except (ValueError, SQLAlchemyError, AssertionError) as error:
        # Exceções SQL podem conter credenciais/parâmetros: não imprimir seu texto.
        if isinstance(error, ValueError):
            print(str(error), file=sys.stderr)
        else:
            print('Verificação falhou. Confira servidor, credenciais e permissões '
                  '(incluindo CREATE TEMPORARY TABLES). Nenhuma URL foi exibida.', file=sys.stderr)
        sys.exit(1)
