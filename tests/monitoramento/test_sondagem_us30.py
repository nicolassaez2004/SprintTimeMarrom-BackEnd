from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
import requests

from app import create_app, db
from app.models import VerificacaoDisponibilidade
from app.monitoramento.sondagem import sondar_e_gravar, verificar


@pytest.fixture
def app():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://"})
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.engine.dispose()


@pytest.fixture
def servidor():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            status = 503 if self.path == "/falha" else 200
            self.send_response(status)
            self.end_headers()
            self.wfile.write(b"painel acessivel")
        def log_message(self, *args):
            pass
    servidor = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=servidor.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{servidor.server_port}"
    servidor.shutdown()
    servidor.server_close()
    thread.join()


@pytest.mark.parametrize("api,painel,esperado", [
    ("/falha", "/ok", [("API", False, 503), ("PAINEL", True, 200)]),
    ("/ok", "/falha", [("API", True, 200), ("PAINEL", False, 503)]),
])
def test_alvos_independentes_e_persistidos(app, servidor, api, painel, esperado):
    sondar_e_gravar(servidor + api, servidor + painel, texto_painel="painel acessivel")
    db.session.expire_all()
    medicoes = VerificacaoDisponibilidade.query.order_by(VerificacaoDisponibilidade.alvo).all()
    assert [(m.alvo, m.sucesso, m.status_http) for m in medicoes] == esperado
    assert all(m.instante.tzinfo and m.duracao_ms >= 0 for m in medicoes)


def test_timeout_conteudo_e_validacao(servidor):
    class Sessao:
        def get(self, *args, **kwargs):
            raise requests.Timeout()
    assert verificar("API", servidor, sessao=Sessao()).resultado == "TIMEOUT"
    assert verificar("PAINEL", servidor, texto_esperado="inexistente").resultado == "CONTEUDO_INESPERADO"
    with pytest.raises(ValueError):
        verificar("API", "http://usuario:senha@localhost/")


def test_migracao_cria_tabela_e_indice(app):
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect

    arquivo = Path(__file__).resolve().parents[2] / "migrations/versions/us30_0025_disponibilidade.py"
    spec = importlib.util.spec_from_file_location("us30_migracao_teste", arquivo)
    migracao = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migracao)
    with db.engine.begin() as conexao:
        VerificacaoDisponibilidade.__table__.drop(conexao)
        with Operations.context(MigrationContext.configure(conexao)):
            migracao.upgrade()
            inspector = inspect(conexao)
            assert "verificacoes_disponibilidade" in inspector.get_table_names()
            assert {i["name"] for i in inspector.get_indexes("verificacoes_disponibilidade")} == {
                "ix_verificacao_disponibilidade_alvo_instante"}
            migracao.downgrade()
            assert "verificacoes_disponibilidade" not in inspect(conexao).get_table_names()
