"""US27-T02: a mesma aplicação de produção (``create_app``), com medição por requisição.

Só para o ensaio de carga: acrescenta o cabeçalho ``Server-Timing`` com o tempo total da
requisição no Flask (``app``), o tempo gasto em SQL (``db``, soma dos cursores) e quantas
instruções SQL foram executadas (``sql``). O restante (``app - db``) é Python: montagem da
resposta e serialização JSON. Não altera consultas, respostas nem configuração.

Uso pelo servidor WSGI: ``waitress-serve --call tests.carga.instrumentado:criar_app``.
"""
import time

from flask import g
from sqlalchemy import event

from app import create_app, db


def criar_app():
    app = create_app()

    @app.before_request
    def _inicio():
        g.carga_inicio, g.carga_db, g.carga_sql = time.perf_counter(), 0.0, 0

    @app.after_request
    def _cabecalho(resposta):
        if hasattr(g, "carga_inicio"):
            total = (time.perf_counter() - g.carga_inicio) * 1000
            resposta.headers["Server-Timing"] = (f"app;dur={total:.1f}, db;dur={g.carga_db * 1000:.1f}, "
                                                 f"sql;desc=\"{g.carga_sql}\"")
        return resposta

    with app.app_context():
        engine = db.engine

    @event.listens_for(engine, "before_cursor_execute")
    def _antes(conexao, cursor, sql, parametros, contexto, varios):
        conexao.info["carga_t0"] = time.perf_counter()

    @event.listens_for(engine, "after_cursor_execute")
    def _depois(conexao, cursor, sql, parametros, contexto, varios):
        inicio = conexao.info.pop("carga_t0", None)
        try:
            if inicio is not None and hasattr(g, "carga_db"):
                g.carga_db += time.perf_counter() - inicio
                g.carga_sql += 1
        except RuntimeError:  # fora de requisição (ex.: inicialização)
            pass

    return app
