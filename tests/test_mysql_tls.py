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
        'read_timeout': 15,
        'write_timeout': 15,
        'init_command': 'SET SESSION max_execution_time=15000',  # US27-T03
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


def test_mysql_interrompe_no_servidor_o_select_abandonado_pelo_cliente():
    """US27-T03: max_execution_time acompanha o read_timeout (15 s por padrão; ajustável)."""
    from app import create_app

    app = create_app({"SQLALCHEMY_DATABASE_URI": "mysql+pymysql://u:p@127.0.0.1/x", "MYSQL_SSL_CA": None})
    argumentos = app.config["SQLALCHEMY_ENGINE_OPTIONS"]["connect_args"]
    assert argumentos["read_timeout"] == 15
    assert argumentos["init_command"] == "SET SESSION max_execution_time=15000"
    longo = create_app({"SQLALCHEMY_DATABASE_URI": "mysql+pymysql://u:p@127.0.0.1/x", "MYSQL_SSL_CA": None,
                        "SQLALCHEMY_ENGINE_OPTIONS": {"connect_args": {"read_timeout": 3600}}})
    assert longo.config["SQLALCHEMY_ENGINE_OPTIONS"]["connect_args"]["init_command"] == (
        "SET SESSION max_execution_time=3600000")
