"""US28-T04: mede a extração mensal de uma linha ponta a ponta, numa base MySQL ISOLADA.

Fluxo medido (diagrama de componentes, fluxo 3): ``POST /api/extracoes`` → ``PedidoExtracao`` +
``ExecucaoProcessamento`` → worker separado (``python -m app.jobs --tipo EXTRACAO_BRUTOS``) →
``ExportService`` → FileStore → ``GET /api/extracoes/{id}`` até ``pronto`` → download do arquivo.

- API em servidor WSGI (waitress), não no servidor de desenvolvimento; worker em outro processo.
- Volume SINTÉTICO, dimensionado pela coleta real de 26/11/2026 (linha mais movimentada: 30
  veículos por ciclo de 30 s). A base nunca é a Aiven: só aceita host local e nome ``us28_medicao*``.
- Durante a extração, usuários simulados consultam outras rotas da API; cada um tem IP próprio
  (``X-Forwarded-For`` atrás de um proxy confiável, como em produção), respeitando 60/min por IP.
- Ensaio separado de reinício: o worker é encerrado à força no meio da geração; um novo worker
  retoma o pedido quando a reserva (15 min, valor do produto) vence. O tempo de espera é medido.

Executar na raiz do backend (a URL vem do ambiente, nunca do repositório):
    US28_MEDICAO_MYSQL_URL=mysql+pymysql://root:***@127.0.0.1:3306/us28_medicao \\
        python -m scripts.medir_extracao_us28 --saida docs/evidencias/US28-T04-medicao.json
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import datetime, timedelta, timezone
import http.client
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit

import psutil
from sqlalchemy.engine import make_url

RAIZ = Path(__file__).resolve().parents[1]
UTC = timezone.utc
LINHA = "99516"            # código sintético da linha pedida
OUTRAS = "99"              # prefixo das demais linhas sintéticas
HOSTS_LOCAIS = {"127.0.0.1", "localhost", "::1"}
META_SEGUNDOS = 15 * 60
ROTAS_CONCORRENTES = ("/health", "/api/saude", "/posicoes/atuais?modo=ultima_coleta",
                      "/api/extracoes/{id}", "/downloads/indicadores/metadados")


# --- Segurança e base ------------------------------------------------------------------

def validar_url(texto):
    """Só MySQL local e base de nome ``us28_medicao*``: nunca a Aiven nem bases da equipe."""
    if not texto:
        raise SystemExit("Defina US28_MEDICAO_MYSQL_URL (MySQL local, base us28_medicao*).")
    url = make_url(texto)
    if not url.drivername.startswith("mysql"):
        raise SystemExit("A medição exige MySQL.")
    if url.host not in HOSTS_LOCAIS:
        raise SystemExit("Só é aceito MySQL local (127.0.0.1/localhost); nunca a base compartilhada.")
    if not (url.database or "").startswith("us28_medicao"):
        raise SystemExit("O nome da base deve começar com us28_medicao.")
    return url


def conectar(url, base=True):
    import pymysql

    return pymysql.connect(host=url.host, port=url.port or 3306, user=url.username, password=url.password,
                           database=url.database if base else None, autocommit=True, charset="utf8mb4")


def preparar_schema(url):
    """Cria a base (se preciso) e aplica TODAS as migrações publicadas (Alembic), como em produção."""
    with conectar(url, base=False) as c, c.cursor() as cur:
        cur.execute(f"CREATE DATABASE IF NOT EXISTS `{url.database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    os.environ["DATABASE_URL"] = url.render_as_string(hide_password=False)
    from flask_migrate import upgrade

    from app import create_app

    app = create_app({"SQLALCHEMY_DATABASE_URI": os.environ["DATABASE_URL"], "MYSQL_SSL_CA": None})
    with app.app_context():
        upgrade(directory=str(RAIZ / "migrations"))
    with conectar(url) as c, c.cursor() as cur:
        cur.execute("SELECT version_num FROM alembic_version")
        return cur.fetchone()[0]


def instante(t):
    return t.astimezone(UTC).replace(tzinfo=None)


def carregar_volume(url, inicio, fim, veiculos_linha, veiculos_outras, intervalo=30):
    """Um ciclo a cada ``intervalo`` s em [inicio, fim): ``veiculos_linha`` posições da linha pedida
    e ``veiculos_outras`` de outras linhas. Idempotente: não recarrega um período já carregado."""
    with conectar(url) as c, c.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM registros_gps WHERE linha_origem=%s AND coletado_em>=%s AND coletado_em<%s",
                    (LINHA, instante(inicio), instante(fim)))
        if cur.fetchone()[0]:
            return {"reaproveitado": True}
        cur.execute("SELECT COALESCE(MAX(id),0) FROM execucoes_coleta")
        proximo_ciclo = cur.fetchone()[0] + 1
        t, ciclos, registros, inicio_carga = inicio, [], [], time.perf_counter()
        total_ciclos = total_registros = 0

        def gravar():
            nonlocal ciclos, registros
            if ciclos:
                cur.executemany("INSERT INTO execucoes_coleta (id, fonte, prevista_em, iniciada_em, finalizada_em, estado, "
                                "tentativas, recebidos, novos_persistidos, confirmada_em) VALUES "
                                "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", ciclos)  # só %s: PyMySQL agrupa as linhas
            if registros:
                cur.executemany("INSERT INTO registros_gps (execucao_id, prefixo_origem, linha_origem, sentido_origem, "
                                "latitude, longitude, horario_gps, coletado_em, payload) VALUES "
                                "(%s,%s,%s,%s,%s,%s,%s,%s,%s)", registros)
            ciclos, registros = [], []

        n_veiculos = veiculos_linha + veiculos_outras
        while t < fim:
            ciclo_id = proximo_ciclo + total_ciclos
            coletado = instante(t)
            ciclos.append((ciclo_id, "OLHO_VIVO", coletado, coletado, coletado + timedelta(seconds=2), "OK", 1,
                           n_veiculos, n_veiculos, coletado + timedelta(seconds=2)))
            for v in range(n_veiculos):
                linha = LINHA if v < veiculos_linha else f"{OUTRAS}{v - veiculos_linha:03d}"
                prefixo = f"9{v:04d}"
                horario = coletado - timedelta(seconds=5 + v % 20)
                lat, lon = -23.55 + (v % 50) * 0.001, -46.63 + ((total_ciclos + v) % 60) * 0.0005
                payload = json.dumps({"p": prefixo, "a": True, "ta": horario.isoformat() + "Z", "py": lat, "px": lon,
                                      "cl": linha, "sintetico": "US28-T04"}, separators=(",", ":"))
                registros.append((ciclo_id, prefixo, linha, "1" if v % 2 else "2", lat, lon, horario, coletado, payload))
            total_ciclos += 1
            total_registros += n_veiculos
            if len(registros) >= 20000:
                gravar()
            t += timedelta(seconds=intervalo)
        gravar()
        return {"reaproveitado": False, "ciclos": total_ciclos, "registros": total_registros,
                "segundos_carga": round(time.perf_counter() - inicio_carga, 1)}


def ambiente(url):
    with conectar(url) as c, c.cursor() as cur:
        cur.execute("SELECT VERSION(), @@innodb_buffer_pool_size, @@max_connections")
        versao, pool, conexoes = cur.fetchone()
        cur.execute("SELECT COUNT(*), MIN(coletado_em), MAX(coletado_em) FROM registros_gps")
        total, minimo, maximo = cur.fetchone()
        cur.execute("SELECT ROUND(SUM(data_length+index_length)/1048576,1) FROM information_schema.tables "
                    "WHERE table_schema=DATABASE()")
        tamanho = float(cur.fetchone()[0] or 0)
    versoes = {}
    for pacote in ("Flask", "SQLAlchemy", "PyMySQL", "pyarrow", "waitress"):
        try:
            versoes[pacote] = importlib.metadata.version(pacote)
        except importlib.metadata.PackageNotFoundError:
            versoes[pacote] = None
    return {
        "mysql": {"versao": versao, "innodb_buffer_pool_mb": int(pool) // 1048576, "max_connections": conexoes,
                  "host": "local (127.0.0.1), base isolada"},
        "servidor_wsgi": "waitress (processo separado)", "worker": "python -m app.jobs --tipo EXTRACAO_BRUTOS",
        "python": platform.python_version(), "pacotes": versoes, "so": platform.platform(),
        "cpu_logicas": psutil.cpu_count(), "memoria_gb": round(psutil.virtual_memory().total / 2**30, 1),
        "volume_na_base": {"registros_gps": total, "de": minimo.isoformat() if minimo else None,
                           "ate": maximo.isoformat() if maximo else None, "tamanho_mb_dados_indices": tamanho},
    }


# --- Processos --------------------------------------------------------------------------

def porta_livre():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def ambiente_processo(url, exportacoes):
    env = {**os.environ, "DATABASE_URL": url.render_as_string(hide_password=False), "MYSQL_SSL_CA": "",
           "EXPORTACOES_DIR": str(exportacoes), "SPTRANS_TOKEN": "", "TEST_DATABASE_URL": "",
           "INDICADORES_TRUSTED_PROXY_CIDRS": "127.0.0.1/32", "PYTHONUNBUFFERED": "1"}
    return env


def iniciar_api(env, porta, log):
    processo = subprocess.Popen(
        [sys.executable, "-m", "waitress", "--listen", f"127.0.0.1:{porta}", "--threads", "16",
         # Sem estas opções o waitress descarta o X-Forwarded-For e todos chegam como 127.0.0.1.
         "--trusted-proxy=127.0.0.1", "--trusted-proxy-headers=x-forwarded-for",
         "--call", "app:create_app"], cwd=RAIZ, env=env, stdout=log, stderr=subprocess.STDOUT)
    for _ in range(120):
        try:
            if requisitar(porta, "GET", "/health")[0] == 200:
                return processo
        except OSError:
            pass
        time.sleep(0.5)
    processo.kill()
    raise RuntimeError("API não respondeu")


def iniciar_worker(env, log):
    return subprocess.Popen([sys.executable, "-m", "app.jobs", "--tipo", "EXTRACAO_BRUTOS", "--intervalo", "1"],
                            cwd=RAIZ, env=env, stdout=log, stderr=subprocess.STDOUT)


def requisitar(porta, metodo, caminho, corpo=None, ip=None, timeout=60):
    conexao = http.client.HTTPConnection("127.0.0.1", porta, timeout=timeout)
    try:
        cabecalhos = {"Accept": "application/json"}
        if corpo is not None:
            cabecalhos["Content-Type"] = "application/json"
        if ip:
            cabecalhos["X-Forwarded-For"] = ip
        conexao.request(metodo, caminho, body=json.dumps(corpo) if corpo is not None else None, headers=cabecalhos)
        resposta = conexao.getresponse()
        dados = resposta.read()
        return resposta.status, dados, dict(resposta.getheaders())
    finally:
        conexao.close()


def percentil(valores, p):
    if not valores:
        return None
    ordenados = sorted(valores)
    return ordenados[max(0, math.ceil(len(ordenados) * p / 100) - 1)]


# --- Medições ---------------------------------------------------------------------------

class Consultas:
    """Usuários simulados consultando a API enquanto a extração roda (cada um com IP próprio)."""

    def __init__(self, porta, usuarios, pedido_id, mes):
        self.porta, self.usuarios, self.pedido_id, self.mes = porta, usuarios, pedido_id, mes
        self.parar, self.amostras = threading.Event(), []
        self._lock = threading.Lock()

    def _usuario(self, n):
        rotas = [r.format(id=self.pedido_id) for r in ROTAS_CONCORRENTES]
        rotas = [f"{r}?mes={self.mes}" if r == "/api/saude" else r for r in rotas]
        i = n
        while not self.parar.is_set():
            rota = rotas[i % len(rotas)]
            i += 1
            inicio = time.perf_counter()
            try:
                status = requisitar(self.porta, "GET", rota, ip=f"10.28.0.{n + 1}", timeout=30)[0]
            except OSError as erro:
                status = type(erro).__name__
            with self._lock:
                self.amostras.append({"rota": rota.split("?")[0].replace(self.pedido_id, "{id}"),
                                      "status": status, "ms": round((time.perf_counter() - inicio) * 1000, 1)})
            self.parar.wait(1.5)  # ≈ 40 consultas/min por usuário, abaixo de 60/min por IP

    def iniciar(self):
        self.threads = [threading.Thread(target=self._usuario, args=(n,), daemon=True) for n in range(self.usuarios)]
        for t in self.threads:
            t.start()

    def encerrar(self):
        self.parar.set()
        for t in self.threads:
            t.join(40)
        tempos = [a["ms"] for a in self.amostras]
        falhas = [a for a in self.amostras if not (isinstance(a["status"], int) and 200 <= a["status"] < 400)]
        por_rota = {}
        for a in self.amostras:
            por_rota.setdefault(a["rota"], []).append(a)
        return {
            "usuarios_simulados": self.usuarios, "consultas": len(self.amostras),
            "atendidas_2xx_3xx": len(self.amostras) - len(falhas), "falhas": len(falhas),
            "status_das_falhas": sorted({str(f["status"]) for f in falhas}),
            "p50_ms": percentil(tempos, 50), "p95_ms": percentil(tempos, 95), "max_ms": max(tempos) if tempos else None,
            "por_rota": {r: {"consultas": len(v), "p95_ms": percentil([x["ms"] for x in v], 95),
                             "status": sorted({str(x["status"]) for x in v})} for r, v in sorted(por_rota.items())},
        }


def esperado(url, inicio, fim, pedido_id):
    with conectar(url) as c, c.cursor() as cur:
        cur.execute("SELECT e.bruto_id_maximo FROM pedidos_extracao p JOIN execucoes_processamento e "
                    "ON e.id = p.execucao_processamento_id WHERE p.id=%s", (pedido_id,))
        snapshot = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM registros_gps WHERE linha_origem=%s AND coletado_em>=%s AND coletado_em<%s "
                    "AND id<=%s", (LINHA, instante(inicio), instante(fim), snapshot))
        return cur.fetchone()[0], snapshot


def historico(url, pedido_id):
    with conectar(url) as c, c.cursor() as cur:
        cur.execute("SELECT e.estado, e.tentativas, e.historico FROM pedidos_extracao p JOIN execucoes_processamento e "
                    "ON e.id = p.execucao_processamento_id WHERE p.id=%s", (pedido_id,))
        estado, tentativas, hist = cur.fetchone()
        return {"estado_job": estado, "tentativas": tentativas,
                "historico": [{k: t.get(k) for k in ("tentativa", "estado", "erro", "registros")}
                              for t in json.loads(hist or "[]")]}


def contar_arquivo(caminho, formato):
    if formato == "PARQUET":
        import pyarrow.parquet as pq

        return pq.ParquetFile(caminho).metadata.num_rows
    with open(caminho, encoding="utf-8-sig", newline="") as arquivo:
        return sum(1 for _ in csv.reader(arquivo)) - 1


def solicitar(porta, inicio, fim, formato):
    corpo = {"linha": LINHA, "inicio": inicio.isoformat(), "fim": fim.isoformat(), "formato": formato}
    t0 = time.perf_counter()
    status, dados, _ = requisitar(porta, "POST", "/api/extracoes", corpo, ip="10.28.1.1")
    if status != 202:
        raise RuntimeError(f"POST /api/extracoes respondeu {status}: {dados[:200]!r}")
    return json.loads(dados), t0, round((time.perf_counter() - t0) * 1000, 1)


def acompanhar(porta, pedido_id, ate, observar=None):
    """GET /api/extracoes/{id} a cada 0,5 s até ``pronto``/``erro`` (ou o limite)."""
    estados, ultimo = [], None
    while time.perf_counter() < ate:
        status, dados, _ = requisitar(porta, "GET", f"/api/extracoes/{pedido_id}", ip="10.28.1.1")
        corpo = json.loads(dados) if status == 200 else {}
        estado = corpo.get("estado")
        if estado != ultimo:
            estados.append({"estado": estado, "em_s": round(time.perf_counter(), 3)})
            ultimo = estado
        if observar and observar(corpo):
            return corpo, estados
        if estado in ("pronto", "erro"):
            return corpo, estados
        time.sleep(0.5)
    return None, estados


def baixar(porta, corpo, destino):
    caminho = urlsplit(corpo["arquivo"]["download"]).path
    conexao = http.client.HTTPConnection("127.0.0.1", porta, timeout=600)
    t0 = time.perf_counter()
    conexao.request("GET", caminho, headers={"X-Forwarded-For": "10.28.1.1"})
    resposta = conexao.getresponse()
    tamanho = 0
    with open(destino, "wb") as arquivo:
        while bloco := resposta.read(1 << 20):
            arquivo.write(bloco)
            tamanho += len(bloco)
    conexao.close()
    return resposta.status, tamanho, round(time.perf_counter() - t0, 2)


def medir_extracao(url, porta, inicio, fim, formato, usuarios, pasta, mes):
    linha_base = Consultas(porta, usuarios, "00000000-0000-0000-0000-000000000000", mes)
    linha_base.iniciar()
    time.sleep(20)
    referencia = linha_base.encerrar()  # mesmas rotas sem extração, para comparar

    corpo, t0, post_ms = solicitar(porta, inicio, fim, formato)
    pedido_id = corpo["id"]
    consultas = Consultas(porta, usuarios, pedido_id, mes)
    consultas.iniciar()
    final, estados = acompanhar(porta, pedido_id, t0 + 2 * META_SEGUNDOS)
    t_pronto = time.perf_counter()
    durante = consultas.encerrar()
    if not final or final.get("estado") != "pronto":
        return {"pedido_id": pedido_id, "estado_final": final and final.get("estado"), "estados": estados,
                "consultas_durante": durante, "conclusao": "a extração não ficou pronta"}
    status, tamanho, download_s = baixar(porta, final, pasta / f"extracao.{formato.lower()}")
    registros_arquivo = contar_arquivo(pasta / f"extracao.{formato.lower()}", formato)
    esperados, snapshot = esperado(url, inicio, fim, pedido_id)
    for e in estados:
        e["em_s"] = round(e["em_s"] - t0, 2)
    return {
        "pedido_id": pedido_id, "formato": formato, "post_202_ms": post_ms, "estados_observados": estados,
        "solicitacao_ate_pronto_s": round(t_pronto - t0, 2), "download": {"status": status, "bytes": tamanho,
                                                                          "segundos": download_s},
        "solicitacao_ate_arquivo_baixado_s": round(t_pronto - t0 + download_s, 2),
        "contagem": {"esperada_sql": esperados, "informada_api": final.get("progresso", {}).get("registros")
                     if isinstance(final.get("progresso"), dict) else None,
                     "lida_no_arquivo": registros_arquivo, "snapshot_bruto_id_maximo": snapshot,
                     "confere": esperados == registros_arquivo},
        "consultas_sem_extracao": referencia, "consultas_durante_extracao": durante,
        "job": historico(url, pedido_id),
    }


def medir_reinicio(url, porta, env, inicio, fim, formato, pasta, log):
    """Ensaio separado: worker morto no meio da geração; outro worker retoma o pedido."""
    worker = iniciar_worker(env, log)
    corpo, t0, _ = solicitar(porta, inicio, fim, formato)
    pedido_id = corpo["id"]
    andamento, estados = acompanhar(
        porta, pedido_id, t0 + 600,
        observar=lambda c: c.get("estado") == "processando" and (c.get("progresso") or {}).get("registros", 0) > 0)
    if not andamento or andamento.get("estado") != "processando":
        worker.kill()
        return {"pedido_id": pedido_id, "conclusao": "não foi possível interromper durante o processamento",
                "estados": estados}
    registros_no_corte = andamento["progresso"]["registros"]
    worker.kill()  # encerramento abrupto (TerminateProcess/SIGKILL)
    worker.wait(30)
    t_morte = time.perf_counter()
    time.sleep(3)
    status_apos, dados, _ = requisitar(porta, "GET", f"/api/extracoes/{pedido_id}", ip="10.28.1.1")
    estado_apos_queda = json.loads(dados).get("estado") if status_apos == 200 else status_apos
    novo = iniciar_worker(env, log)
    try:
        final, estados_retomada = acompanhar(porta, pedido_id, t_morte + META_SEGUNDOS + 15 * 60 + 300)
    finally:
        novo.terminate()
        novo.wait(60)
    if not final or final.get("estado") != "pronto":
        return {"pedido_id": pedido_id, "estado_final": final and final.get("estado"),
                "conclusao": "o pedido não foi recuperado no tempo limite"}
    destino = pasta / f"reinicio.{formato.lower()}"
    status, tamanho, _ = baixar(porta, final, destino)
    lidos = contar_arquivo(destino, formato)
    esperados, _ = esperado(url, inicio, fim, pedido_id)
    for e in estados_retomada:
        e["em_s"] = round(e["em_s"] - t_morte, 2)
    return {
        "pedido_id": pedido_id, "formato": formato, "registros_gerados_antes_da_queda": registros_no_corte,
        "estado_na_api_apos_queda": estado_apos_queda,
        "estados_apos_reinicio": estados_retomada,
        "queda_ate_pronto_s": round(time.perf_counter() - t_morte, 1),
        "reserva_do_produto_min": 15,
        "contagem": {"esperada_sql": esperados, "lida_no_arquivo": lidos, "confere": esperados == lidos,
                     "download_status": status, "bytes": tamanho},
        "job": historico(url, pedido_id),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mes", default="2026-10", help="Mês (São Paulo) extraído; padrão 2026-10")
    parser.add_argument("--veiculos-linha", type=int, default=30,
                        help="Veículos da linha pedida por ciclo (coleta real: máximo 30, linha 516)")
    parser.add_argument("--veiculos-outras", type=int, default=30, help="Posições de outras linhas por ciclo")
    parser.add_argument("--formato", default="PARQUET", choices=("PARQUET", "CSV"))
    parser.add_argument("--usuarios", type=int, default=10, help="Usuários simulados consultando a API")
    parser.add_argument("--sem-reinicio", action="store_true", help="Pula o ensaio de reinício (≈ 20 min)")
    parser.add_argument("--saida", type=Path, default=RAIZ / "docs/evidencias/US28-T04-medicao.json")
    args = parser.parse_args(argv)

    url = validar_url(os.getenv("US28_MEDICAO_MYSQL_URL"))
    from app.core.tempo import FUSO

    ano, mes = map(int, args.mes.split("-"))
    inicio = datetime(ano, mes, 1, tzinfo=FUSO)
    fim = datetime(ano + (mes == 12), mes % 12 + 1, 1, tzinfo=FUSO)
    relatorio = {"tarefa": "US28-T04", "executado_em": datetime.now(UTC).isoformat(),
                 "dados": "SINTÉTICOS (marcados em payload.sintetico), base MySQL local isolada; nunca a Aiven",
                 "cenario": {"linha": LINHA, "periodo": [inicio.isoformat(), fim.isoformat()],
                             "veiculos_da_linha_por_ciclo": args.veiculos_linha,
                             "outras_posicoes_por_ciclo": args.veiculos_outras, "intervalo_coleta_s": 30,
                             "formato": args.formato, "usuarios_simulados": args.usuarios,
                             "base_dimensionamento": "coleta real de 26/11/2026: até 11.348 veículos/ciclo; "
                                                     "linha mais movimentada (516) com 30 veículos/ciclo"},
                 "meta": "extração de um mês de uma linha disponível em até 15 min; contagens conferem; "
                         "API continua atendendo; reinício recupera o pedido"}
    relatorio["schema_alembic"] = preparar_schema(url)
    relatorio["carga"] = carregar_volume(url, inicio, fim, args.veiculos_linha, args.veiculos_outras)
    relatorio["ambiente"] = ambiente(url)

    pasta = Path(tempfile.mkdtemp(prefix="us28-t04-"))
    env = ambiente_processo(url, pasta / "exportacoes")
    porta = porta_livre()
    with open(pasta / "processos.log", "wb") as log:
        api = iniciar_api(env, porta, log)
        try:
            worker = iniciar_worker(env, log)
            try:
                relatorio["extracao"] = medir_extracao(url, porta, inicio, fim, args.formato, args.usuarios,
                                                       pasta, args.mes)
            finally:
                worker.terminate()
                worker.wait(60)
            if not args.sem_reinicio:
                formato_reinicio = "CSV" if args.formato == "PARQUET" else "PARQUET"  # outro pedido, não reaproveita
                relatorio["reinicio"] = medir_reinicio(url, porta, env, inicio, fim, formato_reinicio, pasta, log)
        finally:
            api.terminate()
            api.wait(60)

    e = relatorio.get("extracao", {})
    r = relatorio.get("reinicio")
    criterios = {
        "extracao_ate_15_min": (e.get("solicitacao_ate_arquivo_baixado_s") or math.inf) <= META_SEGUNDOS,
        "contagens_conferem": bool(e.get("contagem", {}).get("confere")),
        "api_atendeu_durante": e.get("consultas_durante_extracao", {}).get("falhas") == 0
                               and e.get("consultas_durante_extracao", {}).get("consultas", 0) > 0,
        "reinicio_recuperado": None if r is None else bool(r.get("contagem", {}).get("confere")),
    }
    relatorio["criterios"] = criterios
    relatorio["resultado"] = "APROVADO" if all(v is not False for v in criterios.values()) else "REPROVADO"
    args.saida.write_text(json.dumps(relatorio, ensure_ascii=False, indent=1, default=str) + "\n", encoding="utf-8")
    print(json.dumps({"resultado": relatorio["resultado"], "criterios": criterios,
                      "solicitacao_ate_pronto_s": e.get("solicitacao_ate_pronto_s")}, ensure_ascii=False))
    return 0 if relatorio["resultado"] == "APROVADO" else 1


if __name__ == "__main__":
    raise SystemExit(main())
