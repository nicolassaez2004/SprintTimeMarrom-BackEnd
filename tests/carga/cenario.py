"""US27: cenário de carga reproduzível — 50 usuários simultâneos nos painéis, HTTP real e MySQL real.

T01 (cenário), T02 (medição) e T04 (verificação por versão). Executar na raiz do backend:

    US27_CARGA_MYSQL_URL=mysql+pymysql://root:***@127.0.0.1:3306/us27_carga \\
        python -m tests.carga.cenario --rotulo antes

- Servidor: waitress (WSGI de produção), processo separado; nunca ``flask run`` nem
  ``app.test_client()`` (que ficam restritos aos testes funcionais).
- Banco: MySQL local ISOLADO (só host local e base ``us27_carga*``; nunca a Aiven), com todas as
  migrações publicadas (Alembic) e a massa sintética de ``tests/carga/massa.py``.
- Usuários: 50 threads, cada uma com IP próprio atrás de um proxy confiável
  (``X-Forwarded-For``, como em produção), pausa de 1,2–2,2 s entre consultas (abaixo de
  60/min por IP). Consultas iguais às do painel (``js/series/modelo.mjs``): /series com linha,
  sentido, tipo, inicio, fim, page e page_size=50, períodos de 1 dia a 12 meses; e a API
  pública /indicadores (até 31 dias).
- Registra: versão do sistema, servidor, banco, volume, duração, erros, conexões, cache (buffer
  pool InnoDB; a aplicação não tem cache), CPU/memória da API e do MySQL, tempo em SQL e em
  Python por requisição (``Server-Timing``) e as instruções SQL mais caras (performance_schema).

Resultado e código de saída (T04):
- ``APROVADO`` (0): p95 ≤ 2 s e p99 ≤ 5 s, sem nenhuma resposta de erro.
- ``FALHA_DO_REQUISITO`` (1): metas não atingidas, ou respostas 5xx/timeouts/conexão recusada.
- ``FALHA_DO_TESTE`` (2): o ensaio não mediu o que promete (base, migração ou servidor fora do
  ar antes da carga, amostra pequena, 429 por cota mal dimensionada, cliente saturado).
"""
import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import http.client
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import random
import socket
import subprocess
import sys
import threading
import time

import psutil
from sqlalchemy.engine import make_url

from tests.carga import massa

RAIZ = Path(__file__).resolve().parents[2]
UTC = timezone.utc
HOSTS_LOCAIS = {"127.0.0.1", "localhost", "::1"}
META_P95_MS, META_P99_MS = 2000, 5000
PERIODOS_DIAS = (1, 7, 30, 90, 180, 365)
AMOSTRA_MINIMA = 500


class FalhaDoTeste(RuntimeError):
    """O ensaio não pôde medir o requisito (não é falha do sistema medido)."""


# --- Base --------------------------------------------------------------------------------

def validar_url(texto, prefixo="us27_carga"):
    if not texto:
        raise FalhaDoTeste(f"Defina a URL do MySQL local (base {prefixo}*).")
    url = make_url(texto)
    if not url.drivername.startswith("mysql"):
        raise FalhaDoTeste("O ensaio exige MySQL real.")
    if url.host not in HOSTS_LOCAIS:
        raise FalhaDoTeste("Só MySQL local e isolado; nunca a base compartilhada.")
    if not (url.database or "").startswith(prefixo):
        raise FalhaDoTeste(f"O nome da base deve começar com {prefixo}.")
    return url


def conectar(url, base=True):
    import pymysql

    return pymysql.connect(host=url.host, port=url.port or 3306, user=url.username, password=url.password,
                           database=url.database if base else None, autocommit=True, charset="utf8mb4")


def preparar_schema(url):
    with conectar(url, base=False) as c, c.cursor() as cur:
        cur.execute(f"CREATE DATABASE IF NOT EXISTS `{url.database}` CHARACTER SET utf8mb4 "
                    "COLLATE utf8mb4_unicode_ci")
    from flask_migrate import upgrade

    from app import create_app

    # Migrações em tabelas grandes (ex.: índice em milhões de indicadores) passam dos 15 s
    # padrão de read_timeout: aplicadas com prazo longo, como o responsável faria na Aiven.
    app = create_app({"SQLALCHEMY_DATABASE_URI": url.render_as_string(hide_password=False), "MYSQL_SSL_CA": None,
                      "SQLALCHEMY_ENGINE_OPTIONS": {"connect_args": {"read_timeout": 3600, "write_timeout": 3600}}})
    with app.app_context():
        upgrade(directory=str(RAIZ / "migrations"))
    with conectar(url) as c, c.cursor() as cur:
        cur.execute("SELECT version_num FROM alembic_version")
        return cur.fetchone()[0]


def volume(url):
    with conectar(url) as c, c.cursor() as cur:
        cur.execute("SELECT COUNT(*), MIN(periodo_inicio), MAX(periodo_fim) FROM indicadores_operacionais")
        n, a, b = cur.fetchone()
        cur.execute("SELECT COUNT(*) FROM execucoes_coleta")
        ciclos = cur.fetchone()[0]
        cur.execute("SELECT table_name, table_rows, ROUND((data_length+index_length)/1048576,1) "
                    "FROM information_schema.tables WHERE table_schema=DATABASE() "
                    "AND table_name IN ('indicadores_operacionais','execucoes_coleta')")
        tabelas = {t: {"linhas_estimadas": r, "mb_dados_indices": float(m)} for t, r, m in cur.fetchall()}
    return {"indicadores_operacionais": n, "de": a.isoformat() if a else None, "ate": b.isoformat() if b else None,
            "execucoes_coleta": ciclos, "tabelas": tabelas}


def status_mysql(url):
    with conectar(url) as c, c.cursor() as cur:
        cur.execute("SHOW GLOBAL STATUS WHERE Variable_name IN ('Threads_connected','Threads_running',"
                    "'Max_used_connections','Innodb_buffer_pool_read_requests','Innodb_buffer_pool_reads',"
                    "'Slow_queries','Questions','Created_tmp_disk_tables','Sort_merge_passes')")
        return {k: int(v) for k, v in cur.fetchall()}


def versoes(url):
    with conectar(url) as c, c.cursor() as cur:
        cur.execute("SELECT VERSION(), @@innodb_buffer_pool_size, @@max_connections, @@transaction_isolation")
        versao, pool, conexoes, isolamento = cur.fetchone()
    commit = subprocess.run(["git", "-C", str(RAIZ), "rev-parse", "HEAD"], capture_output=True, text=True)
    branch = subprocess.run(["git", "-C", str(RAIZ), "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True)
    sujo = subprocess.run(["git", "-C", str(RAIZ), "status", "--porcelain", "--", "app", "migrations"],
                          capture_output=True, text=True)
    pacotes = {}
    for nome in ("Flask", "Flask-SQLAlchemy", "SQLAlchemy", "PyMySQL", "waitress"):
        try:
            pacotes[nome] = importlib.metadata.version(nome)
        except importlib.metadata.PackageNotFoundError:
            pacotes[nome] = None
    return {
        "sistema": {"commit": commit.stdout.strip() or None, "branch": branch.stdout.strip() or None,
                    "app_ou_migracoes_alterados_sem_commit": bool(sujo.stdout.strip())},
        "mysql": {"versao": versao, "innodb_buffer_pool_mb": int(pool) // 1048576, "max_connections": conexoes,
                  "isolamento": isolamento, "host": "local (127.0.0.1), base isolada"},
        "python": platform.python_version(), "pacotes": pacotes, "so": platform.platform(),
        "cpu_logicas": psutil.cpu_count(), "memoria_gb": round(psutil.virtual_memory().total / 2**30, 1),
    }


def zerar_digests(url):
    with conectar(url) as c, c.cursor() as cur:
        cur.execute("TRUNCATE TABLE performance_schema.events_statements_summary_by_digest")


def digests(url, limite=8):
    with conectar(url) as c, c.cursor() as cur:
        cur.execute("SELECT DIGEST_TEXT, COUNT_STAR, ROUND(SUM_TIMER_WAIT/1e9,1), ROUND(AVG_TIMER_WAIT/1e9,2), "
                    "ROUND(MAX_TIMER_WAIT/1e9,1), SUM_ROWS_EXAMINED, SUM_ROWS_SENT, SUM_SORT_ROWS, "
                    "SUM_CREATED_TMP_DISK_TABLES, SUM_NO_INDEX_USED FROM "
                    "performance_schema.events_statements_summary_by_digest WHERE SCHEMA_NAME=DATABASE() "
                    "ORDER BY SUM_TIMER_WAIT DESC LIMIT %s", (limite,))
        campos = ("sql", "execucoes", "total_ms", "media_ms", "max_ms", "linhas_examinadas", "linhas_enviadas",
                  "linhas_ordenadas", "tmp_em_disco", "sem_indice")
        return [dict(zip(campos, [float(v) if hasattr(v, "as_integer_ratio") and not isinstance(v, int) else v
                                  for v in linha])) for linha in cur.fetchall()]


# --- Servidor ----------------------------------------------------------------------------

def porta_livre():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def iniciar_api(url, porta, threads, log):
    env = {**os.environ, "DATABASE_URL": url.render_as_string(hide_password=False), "MYSQL_SSL_CA": "",
           "SPTRANS_TOKEN": "", "TEST_DATABASE_URL": "", "INDICADORES_TRUSTED_PROXY_CIDRS": "127.0.0.1/32",
           "PYTHONUNBUFFERED": "1"}
    processo = subprocess.Popen([sys.executable, "-m", "waitress", "--listen", f"127.0.0.1:{porta}",
                                 "--threads", str(threads),
                                 # Como atrás de um proxy de produção: sem isto o waitress descarta o
                                 # X-Forwarded-For e os 50 usuários contariam como um só IP (429).
                                 "--trusted-proxy=127.0.0.1", "--trusted-proxy-headers=x-forwarded-for",
                                 "--call", "tests.carga.instrumentado:criar_app"],
                                cwd=RAIZ, env=env, stdout=log, stderr=subprocess.STDOUT)
    for _ in range(120):
        if processo.poll() is not None:
            raise FalhaDoTeste("O servidor WSGI encerrou antes da carga")
        try:
            if pedir(porta, "/health", "10.27.255.1")["status"] == 200:
                return processo
        except OSError:
            pass
        time.sleep(0.5)
    processo.kill()
    raise FalhaDoTeste("O servidor WSGI não respondeu antes da carga")


def pedir(porta, caminho, ip, timeout=30):
    t0 = time.perf_counter()
    conexao = http.client.HTTPConnection("127.0.0.1", porta, timeout=timeout)
    try:
        conexao.request("GET", caminho, headers={"X-Forwarded-For": ip, "Accept": "application/json"})
        resposta = conexao.getresponse()
        corpo = resposta.read()
        ms = (time.perf_counter() - t0) * 1000
        tempos = {}
        for parte in (resposta.getheader("Server-Timing") or "").split(","):
            nome, _, resto = parte.strip().partition(";")
            if resto.startswith("dur="):
                tempos[nome] = float(resto[4:])
            elif resto.startswith("desc="):
                tempos[nome] = int(resto[5:].strip('"'))
        return {"status": resposta.status, "ms": round(ms, 1), "bytes": len(corpo), "servidor": tempos}
    except OSError as erro:
        return {"status": type(erro).__name__, "ms": round((time.perf_counter() - t0) * 1000, 1), "bytes": 0,
                "servidor": {}}
    finally:
        conexao.close()


# --- Usuários ----------------------------------------------------------------------------

def meia_noite_sp(dia):
    from app.core.tempo import FUSO

    return datetime.combine(dia, datetime.min.time(), FUSO).isoformat()


def consulta_painel(aleatorio, linhas, com_parada, fim_dados, dias_historico=365):
    """Uma consulta como o painel monta: tipo, linha, sentido e período escolhidos pelo usuário."""
    dias = aleatorio.choice(PERIODOS_DIAS)
    ultimo = fim_dados.date() - timedelta(days=aleatorio.randint(0, max(0, dias_historico - 1 - dias)))
    primeiro = ultimo - timedelta(days=dias - 1)
    sentido = aleatorio.choice(("ida", "volta"))
    if com_parada and aleatorio.random() < 0.25:
        linha = aleatorio.choice(com_parada)
        tipo = aleatorio.choice([t for t, _ in massa.POR_PARADA])
        parada = aleatorio.choice(massa.paradas(linha, 3))
    else:
        linha, tipo, parada = aleatorio.choice(linhas), aleatorio.choice([t for t, _ in massa.POR_LINHA]), None
    pagina = 1 if aleatorio.random() < 0.8 else aleatorio.randint(2, 20)
    args = {"linha": linha, "sentido": sentido, "tipo": tipo, "page": pagina, "page_size": 50,
            "inicio": meia_noite_sp(primeiro), "fim": meia_noite_sp(ultimo + timedelta(days=1))}
    if parada:
        args["parada"] = parada
    from urllib.parse import urlencode

    return "/series?" + urlencode(args), {"rota": "/series", "dias": dias, "pagina": pagina,
                                          "por_parada": parada is not None}


def consulta_api(aleatorio, linhas, com_parada, fim_dados, dias_historico=365):
    """API pública /indicadores: até 31 dias, linha e tipo."""
    from urllib.parse import urlencode

    dias = aleatorio.choice((1, 7, 31))
    ultimo = fim_dados.date() - timedelta(days=aleatorio.randint(0, max(0, dias_historico - 1 - dias)))
    primeiro = ultimo - timedelta(days=dias - 1)
    linha = aleatorio.choice(linhas)
    args = {"linha": linha, "tipo": aleatorio.choice([t for t, _ in massa.POR_LINHA]),
            "sentido": aleatorio.choice(("0", "1")), "page_size": 100,
            "inicio": meia_noite_sp(primeiro), "fim": meia_noite_sp(ultimo + timedelta(days=1))}
    return "/indicadores?" + urlencode(args), {"rota": "/indicadores", "dias": dias, "pagina": 1, "por_parada": False}


class Carga:
    def __init__(self, porta, usuarios, duracao, aquecimento, linhas, com_parada, fim_dados, semente,
                 dias_historico=365):
        self.porta, self.usuarios, self.duracao, self.aquecimento = porta, usuarios, duracao, aquecimento
        self.linhas, self.com_parada, self.fim_dados, self.semente = linhas, com_parada, fim_dados, semente
        self.dias_historico = dias_historico
        self.medicoes, self._lock, self.parar = [], threading.Lock(), threading.Event()

    def _usuario(self, n):
        aleatorio = random.Random(self.semente * 1000 + n)
        ip = f"10.27.{n // 250}.{n % 250 + 1}"
        self.parar.wait(aleatorio.uniform(0, 2))  # chegadas espalhadas
        while not self.parar.is_set():
            gerar = consulta_painel if aleatorio.random() < 0.8 else consulta_api
            caminho, descricao = gerar(aleatorio, self.linhas, self.com_parada, self.fim_dados, self.dias_historico)
            inicio = time.monotonic()
            resultado = pedir(self.porta, caminho, ip)
            with self._lock:
                self.medicoes.append({"t": round(inicio - self.t0, 2), "usuario": n, **descricao, "url": caminho,
                                      **resultado})
            self.parar.wait(aleatorio.uniform(1.2, 2.2))

    def executar(self, amostrador):
        self.t0 = time.monotonic()
        threads = [threading.Thread(target=self._usuario, args=(n,), daemon=True) for n in range(self.usuarios)]
        for t in threads:
            t.start()
        amostrador.iniciar(self.t0)
        time.sleep(self.aquecimento + self.duracao)
        self.parar.set()
        for t in threads:
            t.join(60)
        amostrador.encerrar()
        return self.medicoes


class Amostrador:
    """CPU/memória da API, do MySQL e do cliente de carga, e conexões do MySQL, a cada 1 s."""

    def __init__(self, url, pid_api):
        self.url, self.parar, self.amostras = url, threading.Event(), []
        # No Windows o python.exe da venv é um lançador: o servidor real é o processo filho.
        lancador = psutil.Process(pid_api)
        self.api = max([lancador, *lancador.children(recursive=True)], key=lambda p: p.memory_info().rss)
        candidatos = [p for p in psutil.process_iter(["name", "memory_info"])
                      if (p.info["name"] or "").lower().startswith("mysqld")]
        self.mysql = max(candidatos, key=lambda p: p.info["memory_info"].rss) if candidatos else None
        self.cliente = psutil.Process()

    def _laco(self):
        for p in filter(None, (self.api, self.mysql, self.cliente)):
            p.cpu_percent(None)
        with conectar(self.url) as c:
            while not self.parar.wait(1):
                with c.cursor() as cur:
                    cur.execute("SHOW GLOBAL STATUS WHERE Variable_name IN ('Threads_connected','Threads_running')")
                    status = {k: int(v) for k, v in cur.fetchall()}
                amostra = {"t": round(time.monotonic() - self.t0, 1), **status,
                           "api_cpu_pct": self.api.cpu_percent(None),
                           "api_rss_mb": round(self.api.memory_info().rss / 2**20, 1),
                           "cliente_cpu_pct": self.cliente.cpu_percent(None)}
                if self.mysql:
                    amostra["mysql_cpu_pct"] = self.mysql.cpu_percent(None)
                    amostra["mysql_rss_mb"] = round(self.mysql.memory_info().rss / 2**20, 1)
                self.amostras.append(amostra)

    def iniciar(self, t0):
        self.t0 = t0
        self.thread = threading.Thread(target=self._laco, daemon=True)
        self.thread.start()

    def encerrar(self):
        self.parar.set()
        self.thread.join(10)


# --- Estatística -------------------------------------------------------------------------

def percentil(valores, p):
    """Nearest-rank: ordenado[ceil(p/100·N) − 1]."""
    if not valores:
        return None
    ordenados = sorted(valores)
    return ordenados[max(0, math.ceil(len(ordenados) * p / 100) - 1)]


def ok(m):
    return isinstance(m["status"], int) and 200 <= m["status"] < 300


def resumo(medicoes):
    tempos = [m["ms"] for m in medicoes]
    # Erros nunca contam como respostas rápidas: no percentil "com_erros_como_infinito" cada erro vale +∞.
    piores = [m["ms"] if ok(m) else math.inf for m in medicoes]
    servidor = [m for m in medicoes if "app" in m["servidor"]]
    db_ms = [m["servidor"].get("db", 0) for m in servidor]
    python_ms = [m["servidor"]["app"] - m["servidor"].get("db", 0) for m in servidor]
    fila_ms = [m["ms"] - m["servidor"]["app"] for m in servidor]
    return {
        "consultas": len(medicoes), "sucesso_2xx": sum(ok(m) for m in medicoes),
        "erros": len(medicoes) - sum(ok(m) for m in medicoes),
        "status": dict(Counter(str(m["status"]) for m in medicoes)),
        "p50_ms": percentil(tempos, 50), "p95_ms": percentil(tempos, 95), "p99_ms": percentil(tempos, 99),
        "max_ms": max(tempos) if tempos else None,
        "p95_ms_com_erros_como_infinito": percentil(piores, 95), "p99_ms_com_erros_como_infinito": percentil(piores, 99),
        "decomposicao_p95_ms": {"sql": percentil(db_ms, 95), "python_montagem_e_json": percentil(python_ms, 95),
                                "fila_e_rede": percentil(fila_ms, 95)},
        "sql_por_requisicao_p95": percentil([m["servidor"].get("sql", 0) for m in servidor], 95),
        "bytes_p95": percentil([m["bytes"] for m in medicoes], 95),
    }


def agrupar(medicoes, chave):
    grupos = {}
    for m in medicoes:
        grupos.setdefault(chave(m), []).append(m)
    return {str(k): resumo(v) for k, v in sorted(grupos.items(), key=lambda kv: str(kv[0]))}


def recursos(amostras, de):
    sel = [a for a in amostras if a["t"] >= de]

    def pico(campo):
        valores = [a[campo] for a in sel if campo in a]
        return {"media": round(sum(valores) / len(valores), 1), "maximo": max(valores)} if valores else None

    return {campo: pico(campo) for campo in ("api_cpu_pct", "api_rss_mb", "mysql_cpu_pct", "mysql_rss_mb",
                                             "cliente_cpu_pct", "Threads_connected", "Threads_running")}


def classificar(geral, amostras_cliente, medicoes):
    """T04: distingue falha do teste (o ensaio não mediu) de falha do requisito (o sistema não atende)."""
    motivos_teste, motivos_requisito = [], []
    if geral["consultas"] < AMOSTRA_MINIMA:
        motivos_teste.append(f"amostra menor que {AMOSTRA_MINIMA} consultas")
    if geral["status"].get("429"):
        motivos_teste.append("429: cota por IP excedida — o cenário não representa usuários reais")
    if (amostras_cliente or {}).get("maximo", 0) >= 95 * psutil.cpu_count():
        motivos_teste.append("cliente de carga saturado")
    usuarios = {m["usuario"] for m in medicoes}
    if len(usuarios) < 50:
        motivos_teste.append(f"só {len(usuarios)} usuários fizeram consultas")
    erros = {k: v for k, v in geral["status"].items() if k not in ("200", "429")}
    if erros:
        motivos_requisito.append(f"respostas de erro: {erros}")
    if geral["p95_ms"] is not None and geral["p95_ms"] > META_P95_MS:
        motivos_requisito.append(f"p95 {geral['p95_ms']} ms > {META_P95_MS} ms")
    if geral["p99_ms"] is not None and geral["p99_ms"] > META_P99_MS:
        motivos_requisito.append(f"p99 {geral['p99_ms']} ms > {META_P99_MS} ms")
    # Amostra pequena porque o próprio sistema travou (timeouts, 5xx) é falha do requisito, não do teste.
    if motivos_requisito and erros:
        motivos_teste = [m for m in motivos_teste if not m.startswith("amostra menor")]
    if motivos_teste:
        return "FALHA_DO_TESTE", motivos_teste + motivos_requisito
    if motivos_requisito:
        return "FALHA_DO_REQUISITO", motivos_requisito
    return "APROVADO", []


# --- Execução ----------------------------------------------------------------------------

def executar(url, *, usuarios=50, duracao=180, aquecimento=20, threads=16, rotulo="ensaio", semente=27,
             n_linhas=20, saida=None):
    relatorio = {"tarefa": "US27", "rotulo": rotulo, "executado_em": datetime.now(UTC).isoformat(),
                 "dados": "SINTÉTICOS (versao_conjunto=" + massa.CONJUNTO + "), MySQL local isolado; nunca a Aiven",
                 "metas": {"p95_ms": META_P95_MS, "p99_ms": META_P99_MS, "erros": 0}}
    try:
        relatorio["schema_alembic"] = preparar_schema(url)
        from app.core.tempo import FUSO

        fim_dados = datetime(2026, 11, 1, tzinfo=FUSO)
        inicio_dados = datetime(2025, 11, 1, tzinfo=FUSO)
        with conectar(url) as c:
            relatorio["massa"] = massa.carregar(c, inicio_dados, fim_dados, n_linhas=n_linhas)
        relatorio["volume"] = volume(url)
        relatorio["ambiente"] = versoes(url)
    except FalhaDoTeste:
        raise
    except Exception as erro:  # base/migração/massa: o ensaio não começou
        raise FalhaDoTeste(f"preparação falhou: {type(erro).__name__}: {erro}") from erro

    linhas, com_parada = massa.linhas(n_linhas), massa.linhas(n_linhas)[:4]
    porta = porta_livre()
    log_path = (saida.parent if saida else RAIZ) / f".carga-{rotulo}.log"
    with open(log_path, "wb") as log:
        api = iniciar_api(url, porta, threads, log)
        try:
            zerar_digests(url)
            antes = status_mysql(url)
            amostrador = Amostrador(url, api.pid)
            carga = Carga(porta, usuarios, duracao, aquecimento, linhas, com_parada, fim_dados, semente)
            medicoes = carga.executar(amostrador)
            depois = status_mysql(url)
            mais_caras = digests(url)
        finally:
            api.terminate()
            try:
                api.wait(30)
            except subprocess.TimeoutExpired:
                api.kill()

    estaveis = [m for m in medicoes if m["t"] >= aquecimento]
    geral = resumo(estaveis)
    lidas = depois["Innodb_buffer_pool_read_requests"] - antes["Innodb_buffer_pool_read_requests"]
    disco = depois["Innodb_buffer_pool_reads"] - antes["Innodb_buffer_pool_reads"]
    rec = recursos(amostrador.amostras, aquecimento)
    resultado, motivos = classificar(geral, rec.get("cliente_cpu_pct"), estaveis)
    relatorio.update({
        "cenario": {"usuarios": usuarios, "duracao_s": duracao, "aquecimento_s": aquecimento,
                    "pausa_entre_consultas_s": [1.2, 2.2], "periodos_dias": PERIODOS_DIAS,
                    "mistura": "80% painel /series (25% por parada; 20% páginas 2–20), 20% API /indicadores",
                    "servidor": f"waitress, {threads} threads, 1 processo", "semente": semente,
                    "pool_sqlalchemy": "padrão do Flask-SQLAlchemy (pool_size 5 + overflow 10 por processo)",
                    "cache": "aplicação sem cache; buffer pool InnoDB do MySQL"},
        "resultado": resultado, "motivos": motivos,
        "geral": geral,
        "aquecimento": resumo([m for m in medicoes if m["t"] < aquecimento]),
        "por_rota": agrupar(estaveis, lambda m: m["rota"]),
        "por_periodo_dias": agrupar([m for m in estaveis if m["rota"] == "/series"], lambda m: m["dias"]),
        "por_parada": agrupar([m for m in estaveis if m["rota"] == "/series"], lambda m: m["por_parada"]),
        "mais_lentas": sorted(({k: m[k] for k in ("url", "status", "ms", "bytes", "servidor")} for m in estaveis),
                              key=lambda m: -m["ms"])[:10],
        "recursos": rec,
        "mysql": {"conexoes_max_usadas": depois["Max_used_connections"],
                  "buffer_pool_acerto_pct": round(100 * (1 - disco / lidas), 3) if lidas else None,
                  "leituras_de_disco": disco, "consultas_lentas": depois["Slow_queries"] - antes["Slow_queries"],
                  "tmp_em_disco": depois["Created_tmp_disk_tables"] - antes["Created_tmp_disk_tables"]},
        "sql_mais_caro": mais_caras,
    })
    if saida:
        saida.parent.mkdir(parents=True, exist_ok=True)
        saida.write_text(json.dumps(relatorio, ensure_ascii=False, indent=1, default=str) + "\n", encoding="utf-8")
    return relatorio


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rotulo", default="ensaio", help="Nome do ensaio (ex.: antes, depois, v1.2)")
    parser.add_argument("--usuarios", type=int, default=50)
    parser.add_argument("--duracao", type=int, default=180, help="Segundos medidos após o aquecimento")
    parser.add_argument("--aquecimento", type=int, default=20)
    parser.add_argument("--threads", type=int, default=16, help="Threads do waitress")
    parser.add_argument("--saida", type=Path)
    args = parser.parse_args(argv)
    saida = args.saida or RAIZ / "docs/desempenho" / f"US27-{args.rotulo}.json"
    try:
        url = validar_url(os.getenv("US27_CARGA_MYSQL_URL"))
        relatorio = executar(url, usuarios=args.usuarios, duracao=args.duracao, aquecimento=args.aquecimento,
                             threads=args.threads, rotulo=args.rotulo, saida=saida)
    except FalhaDoTeste as erro:
        print(json.dumps({"resultado": "FALHA_DO_TESTE", "motivos": [str(erro)]}, ensure_ascii=False))
        return 2
    g = relatorio["geral"]
    print(json.dumps({"resultado": relatorio["resultado"], "motivos": relatorio["motivos"], "consultas": g["consultas"],
                      "erros": g["erros"], "p95_ms": g["p95_ms"], "p99_ms": g["p99_ms"], "saida": str(saida)},
                     ensure_ascii=False))
    return {"APROVADO": 0, "FALHA_DO_REQUISITO": 1}.get(relatorio["resultado"], 2)


if __name__ == "__main__":
    raise SystemExit(main())
