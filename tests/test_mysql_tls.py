from app import create_app


def test_mysql_tls_verifica_certificado_e_hostname():
    app = create_app({
        'SQLALCHEMY_DATABASE_URI': 'mysql+pymysql://usuario@localhost/sptrans_dev',
        'MYSQL_SSL_CA': 'ca.pem',
        'SQLALCHEMY_ENGINE_OPTIONS': {'connect_args': {'connect_timeout': 7}},
    })
    options = app.config['SQLALCHEMY_ENGINE_OPTIONS']
    assert options['connect_args'] == {
        'connect_timeout': 7,
        'ssl_ca': 'ca.pem',
        'ssl_verify_cert': True,
        'ssl_verify_identity': True,
    }
    assert options['pool_pre_ping'] is True


def test_ca_mysql_nao_afeta_testes_sqlite():
    app = create_app({
        'SQLALCHEMY_DATABASE_URI': 'sqlite://',
        'MYSQL_SSL_CA': 'ca.pem',
    })
    assert not app.config.get('SQLALCHEMY_ENGINE_OPTIONS', {}).get('connect_args')
