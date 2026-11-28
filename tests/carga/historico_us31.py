"""US31-T03/T04B: metas da US27 e da US29 medidas AO MESMO TEMPO sobre o histórico sintético.

Sobre a base gerada por ``scripts/gerar_historico.py`` (US31-T02B), com o manifesto em
``docs/desempenho/US31-T02B-manifesto.json``:

- US27: os mesmos 50 usuários de ``tests/carga/cenario.py`` (HTTP real no waitress, períodos de
  1 dia a 12 meses espalhados por todo o histórico gerado);
- US29: o coletor REAL da SPTrans gravando na mesma base, com falhas de origem injetadas
  (``tests/carga/coletor_com_falhas.py``); ao final, ``medir_gravacao`` (gravação confirmada em
  até 60 s em 99% dos ciclos) e as lacunas registradas.

Executar na raiz do backend:
    US31_HISTORICO_MYSQL_URL=mysql+pymysql://root:***@127.0.0.1:3306/us31_historico \\
        python -m tests.carga.historico_us31 --rotulo T03
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from tests.carga import cenario

RAIZ = Path(__file__).resolve().parents[2]
UTC = timezone.utc


def tamanho_base(url):
    with cenario.conectar(url) as c, c.cursor() as cur:
        cur.execute("SELECT table_name, table_rows, ROUND((data_length+index_length)/1073741824,2) "
                    "FROM information_schema.tables WHERE table_schema=DATABASE() ORDER BY 3 DESC LIMIT 8")
        tabelas = {t: {"linhas_estimadas": int(r or 0), "gb": float(g or 0)} for t, r, g in cur.fetchall()}
    return {"tabelas": tabelas, "gb_total": round(sum(t["gb"] for t in tabelas.values()), 2)}


def medir_coleta(url, inicio, fim):
    from app import create_app, db
    from app.models import ExecucaoColeta, LacunaColeta
    from app.monitoramento.metricas import medir_gravacao

    app = create_app({"SQLALCHEMY_DATABASE_URI": url.render_as_string(hide_password=False), "MYSQL_SSL_CA": None})
    with app.app_context():
        try:
            medicao = medir_gravacao(inicio, fim)
            ciclos = ExecucaoColeta.query.filter(ExecucaoColeta.iniciada_em >= inicio,
                                                 ExecucaoColeta.iniciada_em < fim).all()
            duracoes = sorted(c.duracao_gravacao_ms for c in ciclos if c.duracao_gravacao_ms is not None)
            lacunas = LacunaColeta.query.filter(LacunaColeta.inicio >= inicio - timedelta(minutes=1)).all()
            return {
                **{k: (float(v) if hasattr(v, "is_finite") else v) for k, v in medicao.items()},
                "tentativas_por_ciclo": sorted({c.tentativas for c in ciclos}),
                "ciclos_com_retentativa": sum(1 for c in ciclos if (c.tentativas or 1) > 1),
                "registros_gravados": sum(c.novos_persistidos or 0 for c in ciclos),
                "gravacao_ms": {"p50": cenario.percentil(duracoes, 50), "p95": cenario.percentil(duracoes, 95),
                                "p99": cenario.percentil(duracoes, 99), "max": duracoes[-1] if duracoes else None},
                "lacunas_registradas": [{"inicio": l.inicio.isoformat(), "fim": l.fim.isoformat() if l.fim else None,
                                         "causa": l.causa} for l in lacunas],
            }
        finally:
            db.session.remove()
            db.engine.dispose()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rotulo", default="T03")
    parser.add_argument("--duracao", type=int, default=600, help="Segundos medidos (cobre a queda simulada aos 7 min)")
    parser.add_argument("--aquecimento", type=int, default=30)
    parser.add_argument("--manifesto", type=Path, default=RAIZ / "docs/desempenho/US31-T02B-manifesto.json")
    args = parser.parse_args(argv)
    url = cenario.validar_url(os.getenv("US31_HISTORICO_MYSQL_URL"), prefixo="us31_")
    manifesto = json.loads(args.manifesto.read_text(encoding="utf-8"))
    from scripts.gerar_historico import linhas_sinteticas

    linhas = linhas_sinteticas(manifesto["parametros"]["linhas"])
    inicio_dados, fim_dados = (datetime.fromisoformat(t) for t in manifesto["intervalo_utc"])
    saida = RAIZ / "docs/desempenho" / f"US31-{args.rotulo}.json"
    estado = Path(os.getenv("TEMP", "/tmp")) / f"us31-coleta-{args.rotulo}"
    relatorio = {"tarefa": f"US31-{args.rotulo}", "executado_em": datetime.now(UTC).isoformat(),
                 "base": {"manifesto": str(args.manifesto.relative_to(RAIZ)), **tamanho_base(url)},
                 "ambiente": cenario.versoes(url)}
    porta = cenario.porta_livre()
    with open(saida.with_suffix(".log"), "wb") as log:
        api = cenario.iniciar_api(url, porta, 16, log)
        coletor = subprocess.Popen([sys.executable, "-m", "tests.carga.coletor_com_falhas",
                                    url.render_as_string(hide_password=False), str(estado)],
                                   cwd=RAIZ, stdout=log, stderr=subprocess.STDOUT,
                                   env={**os.environ, "PYTHONUNBUFFERED": "1", "MYSQL_SSL_CA": ""})
        inicio_coleta = datetime.now(UTC)
        try:
            cenario.zerar_digests(url)
            amostrador = cenario.Amostrador(url, api.pid)
            carga = cenario.Carga(porta, 50, args.duracao, args.aquecimento, linhas, [], fim_dados, 31,
                                  dias_historico=(fim_dados - inicio_dados).days)
            medicoes = carga.executar(amostrador)
            mais_caras = cenario.digests(url)
        finally:
            fim_coleta = datetime.now(UTC)
            coletor.terminate()
            api.terminate()
            for p in (coletor, api):
                try:
                    p.wait(60)
                except subprocess.TimeoutExpired:
                    p.kill()
    estaveis = [m for m in medicoes if m["t"] >= args.aquecimento]
    geral = cenario.resumo(estaveis)
    rec = cenario.recursos(amostrador.amostras, args.aquecimento)
    resultado_us27, motivos = cenario.classificar(geral, rec.get("cliente_cpu_pct"), estaveis)
    coleta = medir_coleta(url, inicio_coleta + timedelta(seconds=30), fim_coleta - timedelta(seconds=30))
    relatorio.update({
        "cenario": {"usuarios": 50, "duracao_s": args.duracao, "consultas": "mesma mistura da US27, períodos de "
                    "1 dia a 12 meses espalhados pelo histórico", "coleta": "SPTrans real + falhas injetadas"},
        "us27": {"resultado": resultado_us27, "motivos": motivos, "geral": geral,
                 "por_periodo_dias": cenario.agrupar([m for m in estaveis if m["rota"] == "/series"], lambda m: m["dias"]),
                 "mais_lentas": sorted(({k: m[k] for k in ("url", "status", "ms", "servidor")} for m in estaveis),
                                       key=lambda m: -m["ms"])[:8]},
        "us29": coleta, "recursos": rec, "sql_mais_caro": mais_caras,
    })
    saida.write_text(json.dumps(relatorio, ensure_ascii=False, indent=1, default=str) + "\n", encoding="utf-8")
    print(json.dumps({"us27": resultado_us27, "p95": geral["p95_ms"], "p99": geral["p99_ms"], "erros": geral["erros"],
                      "us29_pct_ate_60s": coleta.get("percentual_ate_60s_sem_falhas_da_fonte"),
                      "saida": str(saida)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
