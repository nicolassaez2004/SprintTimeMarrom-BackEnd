"""US31-T03: coletor REAL da SPTrans com falhas de origem injetadas, contra uma base isolada.

Mesmo processo de produção (``app.coleta.resiliencia.executar_worker``: retentativas com espera
crescente, lacuna registrada, retomada), com uma única troca: o cliente SPTrans falha de propósito
em ciclos escolhidos, para simular instabilidade da origem. As posições gravadas são reais.

Roteiro de falhas (por minuto desde o início, 2 ciclos por minuto):
- a cada 5 min, o primeiro ciclo do minuto falha nas 2 primeiras tentativas (HTTP 503) e se
  recupera na 3ª — exercita "até 5 novas tentativas com espera crescente";
- entre os minutos 7 e 8, todas as tentativas falham — o ciclo vira FALHA e a lacuna é registrada.

Uso: ``python -m tests.carga.coletor_com_falhas <url-mysql-local-us31_*> <pasta-de-estado>``.
"""
import os
from pathlib import Path
import sys
import threading
import time

URL, ESTADO = sys.argv[1], Path(sys.argv[2])
if "@127.0.0.1" not in URL and "@localhost" not in URL:
    raise SystemExit("só MySQL local")
os.environ["DATABASE_URL"] = URL

from app import create_app  # noqa: E402
from app.integrations import sptrans  # noqa: E402

INICIO = time.monotonic()
_tentativas = {}
_lock = threading.Lock()
original = sptrans.SPTransClient.buscar_todas_posicoes


def com_falhas(self):
    minuto = int((time.monotonic() - INICIO) // 60)
    with _lock:
        chave = int((time.monotonic() - INICIO) // 30)  # um ciclo a cada 30 s
        n = _tentativas[chave] = _tentativas.get(chave, 0) + 1
    if 7 <= minuto < 8:
        raise sptrans.SPTransHTTPError("/Posicao", 503)            # queda: esgota as tentativas
    if minuto % 5 == 0 and minuto > 0 and chave % 2 == 0 and n <= 2:
        raise sptrans.SPTransHTTPError("/Posicao", 503)            # instabilidade passageira
    return original(self)


sptrans.SPTransClient.buscar_todas_posicoes = com_falhas
app = create_app({"SQLALCHEMY_DATABASE_URI": URL, "MYSQL_SSL_CA": None, "COLETA_STATE_DIR": str(ESTADO)})
with app.app_context():
    from app.coleta.resiliencia import executar_worker

    executar_worker()
