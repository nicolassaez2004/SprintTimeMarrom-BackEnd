"""Inicia a API local e a coleta contínua de ônibus no MySQL configurado."""
import logging
import os
from pathlib import Path
import subprocess
import sys


RAIZ = Path(__file__).resolve().parent


def executar_no_ambiente_local():
    scripts = Path("Scripts/python.exe") if os.name == "nt" else Path("bin/python")
    python = RAIZ / ".venv" / scripts
    if sys.prefix == sys.base_prefix and python.is_file():
        return subprocess.call([str(python), str(Path(__file__).resolve()), *sys.argv[1:]])
    return None


if __name__ == "__main__":
    os.chdir(RAIZ)
    codigo = executar_no_ambiente_local()
    if codigo is not None:
        raise SystemExit(codigo)

from app import create_app  # noqa: E402

app = create_app()


def iniciar():
    from werkzeug.serving import make_server

    logging.basicConfig(level=logging.INFO)
    app.debug = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    with make_server("127.0.0.1", 5000, app, threaded=True) as servidor:
        coletor = None
        try:
            if app.config.get("SPTRANS_TOKEN"):
                coletor = subprocess.Popen(
                    [sys.executable, "-u", "-m", "app.coleta.bus_collector"],
                    cwd=RAIZ,
                )
                app.logger.info("Coletor de ônibus iniciado junto com o servidor.")
            else:
                app.logger.warning("Configure SPTRANS_TOKEN no .env para coletar ônibus.")
            app.logger.info("Backend disponível em http://127.0.0.1:5000")
            servidor.serve_forever()
        finally:
            if coletor is not None and coletor.poll() is None:
                coletor.terminate()
                coletor.wait()


if __name__ == "__main__":
    iniciar()
