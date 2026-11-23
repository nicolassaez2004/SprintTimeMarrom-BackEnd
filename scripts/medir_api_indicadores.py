"""US11-T03: ensaio HTTP reproduzível com dois processos e SQLite local novo.

Nunca aceita URL de banco. A base e seus 100 mil valores são sintéticos.
Mede até o recebimento completo do corpo, incluindo conexão TCP, sem Flask test_client.
"""

import argparse
import http.client
import importlib.metadata
import json
import logging
import math
import multiprocessing
import os
import platform
import sqlite3
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Thread


CONSULTAS = (
    "/indicadores?page_size=100",
    "/indicadores?linha=8000&indicador=pontualidade&page_size=20",
    "/series?linha=9000&indicador=espera_media&inicio=2026-11-23T00:00:00Z&fim=2026-11-24T00:00:00Z&page_size=100",
    "/series?page=10&page_size=100",
)


def percentil(valores, percentual):
    if not valores:
        raise ValueError("Amostra vazia não possui percentil")
    return sorted(valores)[math.ceil(len(valores) * percentual / 100) - 1]


def _preparar_base(caminho, quantidade):
    from app import create_app, db
    from app.models import IndicadorOperacional, LimiteConsultaIP

    app = create_app({"SQLALCHEMY_DATABASE_URI": "sqlite:///" + caminho.as_posix(), "MYSQL_SSL_CA": None})
    inicio = datetime(2026, 11, 23, tzinfo=timezone.utc)
    with app.app_context():
        db.metadata.create_all(db.engine, tables=[IndicadorOperacional.__table__, LimiteConsultaIP.__table__])
        with db.engine.connect() as conexao:
            conexao.exec_driver_sql("PRAGMA journal_mode=WAL")
            conexao.commit()
        with db.engine.begin() as conexao:
            for posicao in range(0, quantidade, 5000):
                lote = []
                for n in range(posicao, min(posicao + 5000, quantidade)):
                    instante = inicio + timedelta(seconds=n)
                    lote.append({"id": f"ensaio-{n:08d}", "linha_codigo": "8000" if n % 2 == 0 else "9000",
                        "indicador": "pontualidade" if n % 2 == 0 else "espera_media",
                        "valor": 80 + n % 200 / 10 if n % 2 == 0 else 5 + n % 50 / 10,
                        "unidade": "%" if n % 2 == 0 else "min", "periodo_inicio": instante,
                        "periodo_fim": instante + timedelta(seconds=60), "versao_conjunto": "ensaio-us11-v1",
                        "qualidade": {"cobertura": 0.98, "observacoes": 1250}, "publicado_em": inicio})
                conexao.execute(IndicadorOperacional.__table__.insert(), lote)
        db.session.remove()
        db.engine.dispose()


def _worker(caminho, canal):
    from app import create_app, db
    from werkzeug.serving import make_server

    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    app = create_app({"SQLALCHEMY_DATABASE_URI": "sqlite:///" + caminho,
        "MYSQL_SSL_CA": None, "INDICADORES_TRUSTED_PROXY_CIDRS": "",
        "SQLALCHEMY_ENGINE_OPTIONS": {"connect_args": {"timeout": 15}}})
    servidor = make_server("127.0.0.1", 0, app, threaded=True)
    thread = Thread(target=servidor.serve_forever, daemon=True)
    thread.start()
    canal.send({"porta": servidor.server_port, "pid": os.getpid()})
    try:
        canal.recv()
    finally:
        servidor.shutdown()
        servidor.server_close()
        thread.join(timeout=5)
        with app.app_context():
            db.session.remove()
            db.engine.dispose()
        canal.close()


def _pedir(porta, caminho, ip, headers=None):
    inicio = time.perf_counter_ns()
    conexao = http.client.HTTPConnection("127.0.0.1", porta, timeout=15, source_address=(ip, 0))
    try:
        conexao.request("GET", caminho, headers=headers or {})
        resposta = conexao.getresponse()
        corpo = resposta.read()
        duracao = (time.perf_counter_ns() - inicio) / 1_000_000
        return {"status": resposta.status, "ms": round(duracao, 4), "bytes": len(corpo),
                "headers": dict(resposta.getheaders()), "corpo": json.loads(corpo)}
    except Exception as erro:
        return {"status": None, "ms": round((time.perf_counter_ns() - inicio) / 1_000_000, 4),
                "erro": type(erro).__name__}
    finally:
        conexao.close()


def medir(diretorio, *, registros=100000, amostras=100):
    if amostras < 20 or amostras > 200 or registros < 100:
        raise ValueError("Usar 20–200 amostras e pelo menos 100 registros")
    saida = Path(diretorio).resolve()
    saida.mkdir(parents=True, exist_ok=False)
    banco = saida / "indicadores.sqlite"
    _preparar_base(banco, registros)
    contexto = multiprocessing.get_context("spawn")
    processos, canais, workers = [], [], []
    inicio = datetime.now(timezone.utc)
    try:
        for _ in range(2):
            pai, filho = contexto.Pipe()
            processo = contexto.Process(target=_worker, args=(banco.as_posix(), filho), daemon=True)
            processos.append(processo)
            canais.append(pai)
            processo.start()
            filho.close()
            if not pai.poll(30):
                raise RuntimeError("Worker HTTP não iniciou")
            workers.append(pai.recv())
        print(f"HTTP real: 2 processos, {registros} registros sintéticos, {amostras} consultas.", flush=True)

        def consulta(n):
            indice = (n // 4 + n) % 2
            caminho = CONSULTAS[n % len(CONSULTAS)]
            valor = _pedir(workers[indice]["porta"], caminho, f"127.0.0.{2 + n % 4}")
            return {"amostra": n + 1, "worker": indice + 1, "url": caminho,
                    **{k: v for k, v in valor.items() if k not in ("corpo", "headers")}}

        with ThreadPoolExecutor(max_workers=4) as executor:
            medicoes = list(executor.map(consulta, range(amostras)))
        latencias = [r["ms"] for r in medicoes]
        p95 = percentil(latencias, 95)
        print(f"Amostra HTTP: p95={p95:.3f} ms, status={dict(Counter(r['status'] for r in medicoes))}", flush=True)

        # Usa outro IP real de loopback, sem adulterar a identidade dos clientes
        # medidos acima. A cadeia XFF arbitrária deve continuar sendo ignorada.
        margem = min(55, max(5, math.ceil(max(latencias) * 61 / 1000) + 2))
        while 60 - time.time() % 60 < margem:
            print("Aguardando janela suficiente para verificar a 61ª chamada…", flush=True)
            time.sleep(1)
        caminho = "/indicadores?linha=8000&indicador=pontualidade&page_size=1"
        cota = [_pedir(workers[n % 2]["porta"], caminho, "127.0.0.9",
                       {"X-Forwarded-For": f"198.51.100.{n}"}) for n in range(61)]
        bloqueio = cota[-1]
        outro_ip = _pedir(workers[1]["porta"], caminho, "127.0.0.10")
        nova_janela = None
        if bloqueio["status"] == 429:
            espera = int(bloqueio["headers"]["Retry-After"])
            fim_espera = time.monotonic() + espera + 0.2
            print(f"61ª chamada: 429. Aguardando {espera}s para testar uma nova janela real.", flush=True)
            while time.monotonic() < fim_espera:
                time.sleep(min(5, max(0, fim_espera - time.monotonic())))
            nova_janela = _pedir(workers[0]["porta"], caminho, "127.0.0.9")
        funcional = {
            "primeiras_60_status": dict(Counter(r["status"] for r in cota[:60])),
            "61_status": bloqueio["status"],
            "retry_after": bloqueio.get("headers", {}).get("Retry-After"),
            "resposta_429": bloqueio.get("corpo"),
            "outro_ip_status": outro_ip["status"],
            "nova_janela_status": nova_janela["status"] if nova_janela else None,
            "nova_janela_saldo": nova_janela.get("headers", {}).get("X-RateLimit-Remaining") if nova_janela else None,
            "mesma_cota_entre_workers": all(r["status"] == 200 for r in cota[:60]) and bloqueio["status"] == 429,
            "xff_arbitrario_ignorado": bloqueio["status"] == 429,
        }
        grupos = []
        for caminho_grupo in CONSULTAS:
            grupo = [r for r in medicoes if r["url"] == caminho_grupo]
            grupos.append({"url": caminho_grupo, "amostras": len(grupo),
                           "p95_ms": percentil([r["ms"] for r in grupo], 95),
                           "status": dict(Counter(r["status"] for r in grupo))})
        relatorio = {
            "tarefa": "US11-T03", "iniciado_em": inicio.isoformat(),
            "finalizado_em": datetime.now(timezone.utc).isoformat(),
            "dados": {"origem": "SINTETICA", "registros": registros, "linhas": 2,
                      "indicadores": ["pontualidade", "espera_media"], "versao": "ensaio-us11-v1"},
            "ambiente": {"sistema": platform.platform(), "processador": platform.processor(),
                "cpus_logicas": os.cpu_count(), "python": platform.python_version(),
                "flask": importlib.metadata.version("flask"), "werkzeug": importlib.metadata.version("werkzeug"),
                "sqlalchemy": importlib.metadata.version("sqlalchemy"), "sqlite": sqlite3.sqlite_version,
                "journal": "WAL", "http": "Werkzeug WSGI threaded, debug desativado", "processos": 2,
                "concorrencia_clientes": 4, "ips_reais_loopback": ["127.0.0.2", "127.0.0.3", "127.0.0.4", "127.0.0.5"],
                "proxy": "ausente; conexão direta aos workers alternados", "tls": False},
            "metodo": {"percentil": "nearest-rank: ordenado[ceil(0.95*N)-1]", "amostras": amostras,
                "intervalo": "conexão TCP até corpo completo, incluindo fila no servidor; parsing JSON não cronometrado",
                "aquecimento_http": 0, "falhas_excluidas": 0,
                "funcionais_separados": "61 chamadas de cota, outro IP e nova janela, fora da amostra de latência"},
            "resumo": {"status": dict(Counter(r["status"] for r in medicoes)),
                "min_ms": min(latencias), "mediana_ms": percentil(latencias, 50),
                "p95_ms": p95, "max_ms": max(latencias), "alvo_p95_ms": 2000,
                "meta_atendida_no_ensaio": p95 <= 2000 and all(r["status"] == 200 for r in medicoes)},
            "por_consulta": grupos, "limite_http": funcional, "medicoes": medicoes,
            "exemplo_200": outro_ip.get("corpo"),
            "limites": ["ensaio local com SQLite; não é medição da Aiven/MySQL ou da produção",
                "sem TLS, proxy, internet ou falhas injetadas na amostra de latência",
                "dados sintéticos e cache de SO não esvaziado após preparação da base",
                "não mede capacidade máxima nem garante o mesmo percentil com volumes/cargas diferentes"],
            "aceite": "PENDENTE_REVISAO",
        }
        (saida / "resultado.json").write_text(json.dumps(relatorio, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"resumo": relatorio["resumo"], "limite_http": funcional}, ensure_ascii=False, indent=2), flush=True)
        return relatorio
    finally:
        for canal in canais:
            try:
                canal.send("parar")
            except (BrokenPipeError, EOFError, OSError):
                pass
        for processo in processos:
            processo.join(timeout=10)
            if processo.is_alive():
                processo.terminate()
                processo.join(timeout=5)
        for canal in canais:
            canal.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--saida", required=True, type=Path, help="pasta local nova, nunca um banco compartilhado")
    args = parser.parse_args(argv)
    resultado = medir(args.saida)
    funcional = resultado["limite_http"]
    return 0 if (resultado["resumo"]["meta_atendida_no_ensaio"] and funcional["mesma_cota_entre_workers"]
        and funcional["outro_ip_status"] == 200 and funcional["nova_janela_status"] == 200) else 2


if __name__ == "__main__":
    raise SystemExit(main())
