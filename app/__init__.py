from flask import Flask
from flask_cors import CORS
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy

from app.config import Config


# Instancia compartilhada da extensao do banco
db = SQLAlchemy()
migrate = Migrate()


def create_app(config_extra=None):
    # Cria a aplicacao Flask
    app = Flask(__name__)

    # Carrega as configuracoes (config_extra permite sobrescrever nos testes)
    app.config.from_object(Config)
    if config_extra:
        app.config.update(config_extra)

    if not app.config.get("SQLALCHEMY_DATABASE_URI"):
        raise RuntimeError("DATABASE_URL nao configurado no .env")

    # Aplicar TLS após os overrides para não passar opções MySQL aos testes SQLite.
    from sqlalchemy.engine import make_url

    uri = make_url(app.config["SQLALCHEMY_DATABASE_URI"])
    if uri.drivername == "mysql+pymysql":
        options = dict(app.config.get("SQLALCHEMY_ENGINE_OPTIONS") or {})
        connect_args = dict(options.get("connect_args") or {})
        connect_args.setdefault("connect_timeout", 5)
        connect_args.setdefault("read_timeout", 15)
        connect_args.setdefault("write_timeout", 15)
        if app.config.get("MYSQL_SSL_CA"):
            connect_args.update(
                ssl_ca=app.config["MYSQL_SSL_CA"],
                ssl_verify_cert=True,
                ssl_verify_identity=True,
            )
        options["connect_args"] = connect_args
        options.setdefault("pool_pre_ping", True)
        app.config["SQLALCHEMY_ENGINE_OPTIONS"] = options

    # Inicializa o banco
    db.init_app(app)
    # Migrações em migrations/ (`flask --app app db upgrade`).
    migrate.init_app(app, db)

    # Registra os modelos no metadata do SQLAlchemy para criação/migração.
    from app import models  # noqa: F401
    from app.core.limites import configurar_limites
    configurar_limites(app)

    # Permite requisicoes do frontend local
    origens_frontend = ["http://localhost:5173", "http://127.0.0.1:5173"]
    CORS(
        app,
        expose_headers=[
            "ETag", "Retry-After", "X-RateLimit-Limit", "X-RateLimit-Remaining",
            "X-RateLimit-Reset", "X-Fonte", "X-Fonte-URL", "X-Licenca",
            "X-Licenca-URL", "X-Disponibilizado-Em", "X-Total-Registros",
        ],
        resources={
            r"/api/*": {
                "origins": origens_frontend
            },
            r"/partidas": {"origins": origens_frontend},
            r"/posicoes/atuais": {"origins": origens_frontend},
            # Flask-Cors 6 compara padrões sem metacaracteres literalmente;
            # o sufixo regex cobre as sub-rotas públicas desses recursos.
            r"/qualidade(/.*)?": {"origins": origens_frontend},
            r"/documentos(/.*)?": {"origins": origens_frontend},
            r"/downloads/.*": {"origins": origens_frontend},
            r"/indicadores": {"origins": origens_frontend},
            r"/series": {"origins": origens_frontend},
            r"/mapas/indicadores": {"origins": origens_frontend},
        }
    )

    # Rota de saude da aplicacao
    @app.get("/health")
    def health_root():
        return {"status": "ok"}, 200

    # Rota simples para testar o servidor
    @app.get("/api/health")
    def health():
        return {"status": "ok", "message": "Backend Flask funcionando"}, 200

    # Registra as rotas da API
    from app.api.routes import api_bp

    app.register_blueprint(api_bp)
    from app.indicadores.routes import indicadores_bp

    app.register_blueprint(indicadores_bp)
    from app.api.mapas import mapas_bp
    app.register_blueprint(mapas_bp)
    from app.correspondencia.routes import qualidade_bp

    app.register_blueprint(qualidade_bp)
    from app.programacao.routes import partidas_bp
    app.register_blueprint(partidas_bp)
    from app.consultas.routes import posicoes_bp
    app.register_blueprint(posicoes_bp)
    from app.qualidade.routes import qualidade_coleta_bp
    app.register_blueprint(qualidade_coleta_bp)
    from app.documentos.routes import documentos_bp
    app.register_blueprint(documentos_bp)
    from app.exportacoes.routes import downloads_bp
    app.register_blueprint(downloads_bp)

    return app
