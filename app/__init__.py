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
            # US10/US25: nome do arquivo e versão exportada legíveis pelo frontend.
            "Content-Disposition", "X-Versao-Dados", "X-Versao-Publicada-Em",
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
            r"/contexto/clima": {"origins": origens_frontend},
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

    # Rotas HTTP ficam em app/api/; os serviços, em app/<módulo>/.
    from app.api.atrasos import atrasos_bp
    from app.api.contexto import contexto_bp
    from app.api.correspondencias import correspondencias_bp
    from app.api.documentos import documentos_bp
    from app.api.downloads import downloads_bp
    from app.api.indicadores import indicadores_bp
    from app.api.infraestrutura import infraestrutura_bp
    from app.api.mapas import mapas_bp
    from app.api.partidas import partidas_bp
    from app.api.posicoes import posicoes_bp
    from app.api.qualidade import qualidade_coleta_bp
    from app.api.routes import api_bp

    for blueprint in (api_bp, indicadores_bp, infraestrutura_bp, mapas_bp, correspondencias_bp, partidas_bp,
                      posicoes_bp, qualidade_coleta_bp, documentos_bp, downloads_bp, contexto_bp, atrasos_bp):
        app.register_blueprint(blueprint)

    return app
