"""Demonstração SINTÉTICA da US13-T03, somente SQLite em memória/localhost.

Uso: python -m scripts.demonstrar_qualidade_us13 [--fixture ARQUIVO_JSON]
Não usa a DATABASE_URL da equipe nem faz chamadas às fontes externas.
"""

import argparse
import json
import runpy
from datetime import date
from pathlib import Path

from app import create_app, db
from app.qualidade import routes


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", help="Grava a resposta para testes do frontend e encerra")
    args = parser.parse_args()
    cenario = runpy.run_path(str(Path(__file__).resolve().parents[1] / "tests/qualidade/test_relatorio_diario_us13.py"))
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://",
                      "MYSQL_SSL_CA": None, "GPS_COLLECTION_INTERVAL_SECONDS": 30})
    routes.agora_utc = lambda: cenario["instante"](date(2026, 11, 21))

    @app.after_request
    def marcar_sintetico(resposta):
        if resposta.status_code == 200 and resposta.is_json:
            dados = resposta.get_json()
            dados["simulado"] = True
            resposta.set_data(json.dumps(dados, ensure_ascii=False))
        return resposta

    with app.app_context():
        db.create_all()
        cenario["criar_cenario"]()
        if args.fixture:
            resposta = app.test_client().get("/api/qualidade?inicio=2026-11-17&fim=2026-11-20&janelas=true")
            assert resposta.status_code == 200
            destino = Path(args.fixture)
            destino.parent.mkdir(parents=True, exist_ok=True)
            destino.write_text(json.dumps(resposta.get_json(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            db.session.remove()
            db.engine.dispose()
            return
    print("Cenário SINTÉTICO US13-T03: http://127.0.0.1:5000. Nenhuma conexão com a Aiven.")
    app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
