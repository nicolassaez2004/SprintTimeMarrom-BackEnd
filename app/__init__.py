from flask import Flask
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy

from app.config import Config


# Instancia compartilhada da extensao do banco
db = SQLAlchemy()


def create_app(config_extra=None):
    # Cria a aplicacao Flask
    app = Flask(__name__)

    # Carrega as configuracoes (config_extra permite sobrescrever nos testes)
    app.config.from_object(Config)
    if config_extra:
        app.config.update(config_extra)

    if not app.config.get("SQLALCHEMY_DATABASE_URI"):
        raise RuntimeError("DATABASE_URL nao configurado no .env")

    # Inicializa o banco
    db.init_app(app)

    # Permite requisicoes do frontend local
    CORS(
        app,
        resources={
            r"/api/*": {
                "origins": [
                    "http://localhost:5173",
                    "http://127.0.0.1:5173"
                ]
            }
        }
    )

    # Rota simples para testar o servidor
    @app.get("/api/health")
    def health():
        return {
            "status": "ok",
            "message": "Backend Flask funcionando"
        }, 200

    # Registra as rotas da API
    from app.api.routes import api_bp
    app.register_blueprint(api_bp)

    return app