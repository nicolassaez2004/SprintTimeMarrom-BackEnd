"""US31-T01: registra o volume REAL medido e projeta cinco anos de histórico.

Fontes, todas somente leitura:
- ``--base``: MySQL da coleta (``DATABASE_URL``; sessão READ ONLY). Usa só agregados de
  ``execucoes_coleta`` (um registro por ciclo, com ``recebidos`` e ``novos_persistidos``),
  ``lacunas_coleta`` e os tamanhos de ``information_schema`` — nunca varre ``registros_gps``.
- ``--amostra-sqlite``: coleta real contínua mais longa disponível (SQLite local), para a
  distribuição por linha e o tamanho do payload.

A projeção é rastreável: cada número da saída aponta a medição de onde veio. Se a semana real
contínua ainda não existe, a saída diz isso e a projeção é marcada como provisória, a recalibrar
(``python -m scripts.dimensionar_historico_us31`` de novo) quando a semana existir.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3

RAIZ = Path(__file__).resolve().parents[1]
CICLOS_DIA = 24 * 3600 // 30
DIAS_5_ANOS = 1826  # 5 × 365 + 1 bissexto


def _mysql(url_texto, ca):
    import pymysql
    from sqlalchemy.engine import make_url

    url = make_url(url_texto)
    conexao = pymysql.connect(host=url.host, port=url.port or 3306, user=url.username, password=url.password,
                              database=url.database, ssl={"ca": ca} if ca else None, connect_timeout=15,
                              read_timeout=120)
    with conexao.cursor() as cur:
        cur.execute("SET SESSION TRANSACTION READ ONLY")
    return conexao, url


def medicao_base(conexao):
    with conexao.cursor() as cur:
        cur.execute("SELECT DATE(CONVERT_TZ(iniciada_em,'+00:00','-03:00')) dia, COUNT(*), SUM(estado='OK'), "
                    "SUM(estado<>'OK'), COALESCE(SUM(recebidos),0), COALESCE(SUM(novos_persistidos),0), "
                    "MIN(iniciada_em), MAX(iniciada_em) FROM execucoes_coleta GROUP BY dia ORDER BY dia")
        dias = [{"dia": str(d), "ciclos": c, "ciclos_ok": int(ok or 0), "ciclos_com_falha": int(f or 0),
                 "recebidos": int(r), "novos_persistidos": int(n),
                 "primeiro_ciclo_utc": a.isoformat() if a else None, "ultimo_ciclo_utc": b.isoformat() if b else None,
                 "cobertura_do_dia_pct": round(100 * c / CICLOS_DIA, 2)}
                for d, c, ok, f, r, n, a, b in cur.fetchall()]
        cur.execute("SELECT HOUR(CONVERT_TZ(iniciada_em,'+00:00','-03:00')) h, COUNT(*), AVG(recebidos), "
                    "AVG(novos_persistidos) FROM execucoes_coleta WHERE estado='OK' GROUP BY h ORDER BY h")
        por_hora = [{"hora_sp": h, "ciclos": c, "recebidos_medio": round(float(r or 0)), "novos_medio": round(float(n or 0))}
                    for h, c, r, n in cur.fetchall()]
        cur.execute("SELECT causa, COUNT(*) FROM lacunas_coleta GROUP BY causa ORDER BY 2 DESC")
        lacunas = {str(c): n for c, n in cur.fetchall()}
        cur.execute("SELECT table_name, table_rows, data_length, index_length FROM information_schema.tables "
                    "WHERE table_schema=DATABASE() AND table_name IN ('registros_gps','observacoes_tratadas',"
                    "'indicadores_operacionais','execucoes_coleta')")
        tabelas = {t: {"linhas_estimadas": int(r or 0), "dados_mb": round((d or 0) / 2**20, 1),
                       "indices_mb": round((i or 0) / 2**20, 1)} for t, r, d, i in cur.fetchall()}
    return {"dias": dias, "por_hora": por_hora, "lacunas_por_causa": lacunas, "tabelas": tabelas}


def medicao_amostra(caminho):
    c = sqlite3.connect(f"file:{Path(caminho).as_posix()}?mode=ro", uri=True)
    ciclos = c.execute("SELECT id, iniciada_em, recebidos, novos_persistidos, estado FROM execucoes_coleta "
                       "ORDER BY iniciada_em").fetchall()
    maior = c.execute("SELECT execucao_id FROM registros_gps GROUP BY execucao_id ORDER BY COUNT(*) DESC LIMIT 1").fetchone()[0]
    por_linha = sorted(n for _, n in c.execute("SELECT linha_origem, COUNT(*) FROM registros_gps WHERE execucao_id=? "
                                                "GROUP BY linha_origem", (maior,)))
    payload = c.execute("SELECT AVG(LENGTH(payload)) FROM registros_gps").fetchone()[0]
    total = c.execute("SELECT COUNT(*) FROM registros_gps").fetchone()[0]
    novos = [n for *_, n, _e in ciclos if n is not None]
    return {
        "periodo_utc": [ciclos[0][1], ciclos[-1][1]], "ciclos": len(ciclos),
        "ciclos_por_estado": dict(Counter(e for *_, e in ciclos)),
        "duracao_do_periodo_h": round((datetime.fromisoformat(ciclos[-1][1]) - datetime.fromisoformat(ciclos[0][1]))
                                      .total_seconds() / 3600, 2),
        "horas_equivalentes_a_ciclos_de_30s": round(len(ciclos) * 30 / 3600, 2), "registros": total,
        "recebidos_por_ciclo_medio": round(sum(r for _, _, r, _, _ in ciclos if r) / len(ciclos)),
        "novos_por_ciclo": {"medio": round(sum(novos) / len(novos)), "minimo": min(novos), "maximo": max(novos)},
        "payload_bytes_medio": round(payload, 1),
        "linhas_no_maior_ciclo": len(por_linha),
        "veiculos_por_linha_no_maior_ciclo": {"mediana": por_linha[len(por_linha) // 2],
                                              "p95": por_linha[int(len(por_linha) * 0.95)], "maximo": por_linha[-1]},
    }


def medicao_mes_real(pasta):
    """Coleta real de um mês inteiro em Parquet (um arquivo por dia; colunas c, cl, sl, p, ta,
    py, px). Conta, por dia: posições no arquivo, posições únicas por (prefixo, horário GPS) —
    o que a coleta grava (repetida não é gravada de novo) —, posições com horário GPS fora do
    dia, linhas e veículos distintos; e a distribuição por hora de São Paulo."""
    import pyarrow.compute as pc
    import pyarrow.parquet as pq
    from collections import Counter as Contador
    from datetime import date

    dias, horas, por_linha = [], Contador(), Contador()
    for arquivo in sorted(Path(pasta).glob("*.parquet")):
        dia = date.fromisoformat(arquivo.stem)
        tabela = pq.read_table(arquivo, columns=["cl", "p", "ta"])
        unicas = tabela.group_by(["p", "ta"]).aggregate([])
        ta = pc.strptime(tabela["ta"], format="%Y-%m-%dT%H:%M:%SZ", unit="s", error_is_null=True)
        horas_utc = pc.hour(ta)
        fora = pc.sum(pc.cast(pc.not_equal(pc.utf8_slice_codeunits(tabela["ta"], 0, 10), dia.isoformat()),
                              "int64")).as_py() or 0
        dias.append({"dia": dia.isoformat(), "dia_semana": dia.strftime("%a"), "posicoes_no_arquivo": tabela.num_rows,
                     "posicoes_unicas": unicas.num_rows, "posicoes_com_gps_de_outro_dia": fora,
                     "linhas": pc.count_distinct(tabela["cl"]).as_py(), "veiculos": pc.count_distinct(tabela["p"]).as_py()})
        for h, n in Contador(horas_utc.to_pylist()).items():
            if h is not None:
                horas[(h - 3) % 24] += n
        if dia.day == 4:  # um dia útil completo para a distribuição por linha
            contagem = tabela.group_by("cl").aggregate([("p", "count")])
            por_linha = sorted(contagem.column("p_count").to_pylist())
    unicas = [d["posicoes_unicas"] for d in dias]
    return {"pasta": Path(pasta).name, "dias": dias, "dias_medidos": len(dias),
            "posicoes_unicas_por_dia": {"media": round(sum(unicas) / len(unicas)), "minimo": min(unicas),
                                         "maximo": max(unicas)},
            "posicoes_por_hora_sp": {h: horas[h] for h in range(24)},
            "posicoes_por_linha_em_04_05": {"linhas": len(por_linha), "mediana": por_linha[len(por_linha) // 2],
                                            "p95": por_linha[int(len(por_linha) * 0.95)], "maximo": por_linha[-1]}
            if por_linha else None}


def projetar_com_mes_real(mes, bytes_por_registro):
    por_dia = mes["posicoes_unicas_por_dia"]["media"]
    total = por_dia * DIAS_5_ANOS
    return {"origem": f"média de posições únicas por dia em {mes['dias_medidos']} dias reais ({mes['pasta']})",
            "registros_gps_por_dia": por_dia, "registros_gps_5_anos": total,
            "armazenamento_registros_gps_5_anos_tb": round(total * bytes_por_registro / 2**40, 2)
            if bytes_por_registro else None, "ciclos_coleta_5_anos": CICLOS_DIA * DIAS_5_ANOS,
            "situacao": "RASTREAVEL_A_MEDICAO_REAL (semana real disponível: mês completo de maio/2026)"}


def projetar(base, amostra):
    tab = base["tabelas"].get("registros_gps", {})
    linhas_base = tab.get("linhas_estimadas") or 0
    bytes_por_registro = ((tab.get("dados_mb", 0) + tab.get("indices_mb", 0)) * 2**20 / linhas_base) if linhas_base else None
    dias_continuos = [d for d in base["dias"] if d["cobertura_do_dia_pct"] >= 99]
    novos_ciclo = amostra["novos_por_ciclo"]["medio"]
    cenarios = {}
    for nome, fator in (("limite_superior_dia_inteiro_no_ritmo_diurno", 1.0),
                        ("noite_com_metade_da_frota", 0.75)):
        por_dia = round(novos_ciclo * CICLOS_DIA * fator)
        total = por_dia * DIAS_5_ANOS
        cenarios[nome] = {
            "fator_sobre_ritmo_diurno": fator, "registros_gps_por_dia": por_dia, "registros_gps_5_anos": total,
            "armazenamento_registros_gps_5_anos_tb": round(total * bytes_por_registro / 2**40, 2) if bytes_por_registro else None,
            "ciclos_coleta_5_anos": CICLOS_DIA * DIAS_5_ANOS,
        }
    return {
        "semana_real_continua_existe": bool(len(dias_continuos) >= 7),
        "dias_com_cobertura_continua": len(dias_continuos),
        "bytes_por_registro_mysql_medidos": round(bytes_por_registro, 1) if bytes_por_registro else None,
        "origem_bytes_por_registro": "information_schema de registros_gps (dados + índices) ÷ linhas estimadas",
        "novos_por_ciclo_usado": novos_ciclo, "origem_novos_por_ciclo": "amostra contínua (média de novos_persistidos)",
        "ciclos_por_dia": CICLOS_DIA, "dias_5_anos": DIAS_5_ANOS, "cenarios": cenarios,
        "situacao": ("RASTREAVEL_A_MEDICAO_REAL" if len(dias_continuos) >= 7 else
                     "PROVISORIA: sem semana real contínua; ritmo noturno não medido. Recalibrar quando houver "
                     "7 dias com cobertura ≥ 99%."),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default=os.getenv("DATABASE_URL"), help="MySQL da coleta (somente leitura)")
    parser.add_argument("--ca", default=os.getenv("MYSQL_SSL_CA"))
    parser.add_argument("--amostra-sqlite", type=Path, help="Coleta real contínua (SQLite)")
    parser.add_argument("--mes-parquet", type=Path, help="Pasta com um mês real de posições (um Parquet por dia)")
    parser.add_argument("--anterior", type=Path,
                        help="Relatório já gerado: reaproveita a medição da base e da amostra (não acessa o MySQL)")
    parser.add_argument("--saida", type=Path, default=RAIZ / "docs/desempenho/US31-T01-volume.json")
    args = parser.parse_args(argv)
    if args.anterior:
        relatorio = json.loads(args.anterior.read_text(encoding="utf-8"))
        relatorio["recalibrado_em"] = datetime.now(timezone.utc).isoformat()
    elif args.amostra_sqlite:
        relatorio = _medir(args)
    else:
        parser.error("informe --amostra-sqlite (medição nova) ou --anterior (reaproveitar)")
    if args.mes_parquet:
        relatorio["fontes"]["mes_real"] = {"pasta": args.mes_parquet.name,
                                           "origem": "pasta Dados_GPS_Onibus compartilhada pelo Insper (202605.tar.gz)"}
        relatorio["medicao_mes_real"] = medicao_mes_real(args.mes_parquet)
        relatorio["projecao_recalibrada"] = projetar_com_mes_real(
            relatorio["medicao_mes_real"], relatorio["projecao"]["bytes_por_registro_mysql_medidos"])
    args.saida.parent.mkdir(parents=True, exist_ok=True)
    args.saida.write_text(json.dumps(relatorio, ensure_ascii=False, indent=1, default=str) + "\n", encoding="utf-8")
    print(json.dumps(relatorio.get("projecao_recalibrada") or relatorio["projecao"], ensure_ascii=False, indent=1))
    return 0


def _medir(args):
    conexao, url = _mysql(args.base, args.ca)
    with conexao:
        base = medicao_base(conexao)
    amostra = medicao_amostra(args.amostra_sqlite)
    return {"tarefa": "US31-T01", "executado_em": datetime.now(timezone.utc).isoformat(),
            "fontes": {"base_coleta": {"host": "Aiven (MySQL gerenciado da equipe)" if (url.host or "").endswith(
                "aivencloud.com") else "MySQL informado", "base": url.database, "acesso": "SESSION READ ONLY",
                "tabelas_lidas": ["execucoes_coleta", "lacunas_coleta", "information_schema.tables"]},
                       "amostra_continua": {"arquivo": args.amostra_sqlite.name,
                                            "origem": "coleta real SPTrans gravada localmente (modo local temporário)"}},
            "medicao_base": base, "medicao_amostra": amostra, "projecao": projetar(base, amostra)}


if __name__ == "__main__":
    raise SystemExit(main())
