"""Validação de ponta a ponta da cadeia com dados REAIS (GPS de maio/2026 e GTFS da SPTrans).

Base MySQL local ISOLADA (host local, nome ``cadeia_real*``); nunca a Aiven. Fontes (pasta
``Dados_GPS_Onibus`` compartilhada pelo Insper):

- GTFS: ``cittamobi_gtfs.zip`` (SPTrans, versão 26/02/2026, calendário até 01/09/2026);
- GPS: ``202605/AAAA-MM-DD.parquet`` (colunas c, cl, sl, p, ta, py, px).

Passos, todos pelo código do produto:

1. migrações (Alembic); captura do GTFS (``capturar_gtfs``) e importação da rede
   (``importar_linhas_paradas``, ``importar_trechos``);
2. tabela de correspondência (US03, ``importar_tabela``) construída do próprio GPS: cada
   ``(cl, sl)`` aponta à linha GTFS de mesmo letreiro (``c``), sentido 1→0 e 2→1; letreiro
   sem linha no GTFS fica sem vínculo, com motivo;
3. brutos do período escolhido em ``registros_gps``. **Limitação declarada**: o arquivo não
   tem o horário da coleta; ``coletado_em`` recebe o fim do ciclo de 30 s do horário GPS
   (idade de 0 a 30 s), e os ciclos (``execucoes_coleta``) são um a cada 30 s;
4. ``processar_cadeia`` (partidas do dia, correspondência, reconstrução) em execuções
   sucessivas, como a tarefa periódica faria, e ``consolidar_dia`` (agenda US23-T03);
5. relatório: correspondência por motivo, viagens, cobertura, associação, passagens e
   indicadores publicados.

Uso: ``CADEIA_REAL_MYSQL_URL=... python -m scripts.validar_cadeia_real --dia 2026-05-04
--inicio 06:00 --fim 09:00 [--linhas 20]``.
"""
import argparse
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
import json
import os
from pathlib import Path
import time as relogio

from sqlalchemy.engine import make_url

RAIZ = Path(__file__).resolve().parents[1]
DADOS = Path(os.getenv("LOCALAPPDATA", "")) / "TimeMarrom" / "dados-externos"
UTC = timezone.utc


class _ClienteArquivo:
    def __init__(self, caminho):
        self.caminho = caminho

    def baixar_para(self, url, destino):
        destino.write(Path(self.caminho).read_bytes())


def _url():
    url = make_url(os.environ["CADEIA_REAL_MYSQL_URL"])
    if url.host not in ("127.0.0.1", "localhost") or not (url.database or "").startswith("cadeia_real"):
        raise SystemExit("Só MySQL local e base cadeia_real*.")
    return url


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dia", type=date.fromisoformat, default=date(2026, 5, 4))
    parser.add_argument("--inicio", default="06:00")
    parser.add_argument("--fim", default="09:00")
    parser.add_argument("--linhas", type=int, default=0, help="Limita às N linhas com mais posições (0 = todas)")
    parser.add_argument("--saida", type=Path, default=RAIZ / "docs/evidencias/cadeia-real-2026-05-04.json")
    args = parser.parse_args(argv)
    url = _url()
    import pymysql

    with pymysql.connect(host=url.host, port=url.port or 3306, user=url.username, password=url.password,
                         autocommit=True) as c, c.cursor() as cur:
        cur.execute(f"CREATE DATABASE IF NOT EXISTS `{url.database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")

    from flask_migrate import upgrade

    from app import create_app, db
    from app.core.tempo import FUSO

    longo = {"connect_args": {"read_timeout": 3600, "write_timeout": 3600}}
    app = create_app({"SQLALCHEMY_DATABASE_URI": url.render_as_string(hide_password=False), "MYSQL_SSL_CA": None,
                      "SQLALCHEMY_ENGINE_OPTIONS": longo,
                      "MUNICIPIO_GEOJSON": str(RAIZ / "instance" / "municipio-3550308.geojson"),
                      "GTFS_STORAGE_DIR": str(DADOS / "cadeia-real" / "gtfs"),
                      "EXPORTACOES_DIR": str(DADOS / "cadeia-real" / "exportacoes")})
    relatorio = {"tarefa": "Validação da cadeia com dados reais", "executado_em": datetime.now(UTC).isoformat(),
                 "dia": args.dia.isoformat(), "faixa_sp": [args.inicio, args.fim], "tempos_s": {}}
    marcar = lambda nome, t0: relatorio["tempos_s"].__setitem__(nome, round(relogio.perf_counter() - t0, 1))  # noqa: E731
    with app.app_context():
        t0 = relogio.perf_counter()
        upgrade(directory=str(RAIZ / "migrations"))
        marcar("migracoes", t0)

        from app.gtfs.service import capturar_gtfs, importar_linhas_paradas, importar_trechos
        from app.models import (ExecucaoColeta, IndicadorOperacional, Linha, ObservacaoTratada, PassagemEstimada,
                                RegistroBrutoGPS, TabelaCorrespondencia, ViagemObservada)

        t0 = relogio.perf_counter()
        inicio_mes = datetime.combine(args.dia.replace(day=1), time(), FUSO).astimezone(UTC)
        versao = capturar_gtfs(url="arquivo-local", cliente=_ClienteArquivo(DADOS / "cittamobi_gtfs.zip.d" /
                                                                             "cittamobi_gtfs.zip"),
                               agora=inicio_mes)
        if not Linha.query.filter_by(versao_gtfs_id=versao.id).first():
            relatorio["rede"] = importar_linhas_paradas(versao.id)
            relatorio["rede"].update(importar_trechos(versao.id))
            db.session.commit()
        relatorio["gtfs"] = {"versao_id": versao.id, "hash": versao.hash,
                             "vigencia": [versao.vigencia_inicio.isoformat(),
                                          versao.vigencia_fim.isoformat() if versao.vigencia_fim else None],
                             "linhas": Linha.query.filter_by(versao_gtfs_id=versao.id).count()}
        marcar("gtfs_e_rede", t0)

        import pyarrow as pa
        import pyarrow.compute as pc
        import pyarrow.parquet as pq

        t0 = relogio.perf_counter()
        tabela = pq.read_table(DADOS / "gps-202605" / "202605" / f"{args.dia.isoformat()}.parquet")
        a = datetime.combine(args.dia, time.fromisoformat(args.inicio), FUSO).astimezone(UTC)
        b = datetime.combine(args.dia, time.fromisoformat(args.fim), FUSO).astimezone(UTC)
        ta = pc.strptime(tabela["ta"], format="%Y-%m-%dT%H:%M:%SZ", unit="s", error_is_null=True)
        instante = lambda d: pa.scalar(d.replace(tzinfo=None), type=pa.timestamp("s"))  # noqa: E731
        tabela = tabela.append_column("t", ta).filter(pc.and_(pc.greater_equal(ta, instante(a)), pc.less(ta, instante(b))))
        if args.linhas:
            mais = sorted(Counter(tabela["cl"].to_pylist()).items(), key=lambda kv: -kv[1])[:args.linhas]
            tabela = tabela.filter(pc.is_in(tabela["cl"], value_set=pa.array([k for k, _ in mais])))
        linhas_gtfs = {c for (c,) in db.session.query(Linha.codigo).filter_by(versao_gtfs_id=versao.id)}
        vinculos = {}
        for cl, sl, letreiro in zip(tabela["cl"].to_pylist(), tabela["sl"].to_pylist(), tabela["c"].to_pylist()):
            vinculos.setdefault((str(cl), str(sl)), letreiro)
        nome_tabela = f"letreiro-{args.dia:%Y%m}-v1"
        if not TabelaCorrespondencia.query.filter_by(versao=nome_tabela).first():
            from app.correspondencia.tabelas import importar_tabela

            entradas = [{"codigo_olho_vivo": cl, "sentido_olho_vivo": sl,
                         **({"linha_codigo": letreiro, "sentido_gtfs": "0" if sl == "1" else "1"}
                            if letreiro in linhas_gtfs else
                            {"linha_codigo": None, "sentido_gtfs": None, "motivo": "LETREIRO_SEM_LINHA_GTFS"})}
                        for (cl, sl), letreiro in sorted(vinculos.items()) if sl in ("1", "2")]
            importar_tabela({"versao": nome_tabela, "versao_gtfs_id": versao.id,
                             "fonte": "GPS maio/2026 (campo c = route_id GTFS); construída para a validação",
                             "vigencia_inicio": args.dia.replace(day=1).isoformat(),
                             "vigencia_fim": versao.vigencia_fim.isoformat() if versao.vigencia_fim else None,
                             "entradas": entradas})
            db.session.commit()
        relatorio["tabela_correspondencia"] = {"versao": nome_tabela, "vinculos": len(vinculos),
                                               "sem_linha_gtfs": sum(1 for v in vinculos.values() if v not in linhas_gtfs)}

        if not RegistroBrutoGPS.query.first():
            ciclos, ids = {}, Counter()
            unicos = set()
            registros = []
            for cl, sl, p, t, py, px in zip(tabela["cl"].to_pylist(), tabela["sl"].to_pylist(), tabela["p"].to_pylist(),
                                            tabela["t"].to_pylist(), tabela["py"].to_pylist(), tabela["px"].to_pylist()):
                t = t.replace(tzinfo=UTC)
                if (p, t) in unicos:
                    continue
                unicos.add((p, t))
                fim_ciclo = t - timedelta(seconds=t.second % 30) + timedelta(seconds=30)
                ciclos.setdefault(fim_ciclo, None)
                registros.append((fim_ciclo, str(p), str(cl), str(sl), py, px, t))
            mapa = {}
            for fim_ciclo in sorted(ciclos):
                e = ExecucaoColeta(fonte="OLHO_VIVO", prevista_em=fim_ciclo - timedelta(seconds=30),
                                   iniciada_em=fim_ciclo - timedelta(seconds=30), finalizada_em=fim_ciclo,
                                   estado="OK", tentativas=1, recebidos=0, novos_persistidos=0, confirmada_em=fim_ciclo)
                db.session.add(e)
                mapa[fim_ciclo] = e
            db.session.flush()
            from sqlalchemy import insert

            lote = []
            for fim_ciclo, p, cl, sl, py, px, t in registros:
                lote.append({"execucao_id": mapa[fim_ciclo].id, "prefixo_origem": p, "linha_origem": cl,
                             "sentido_origem": sl, "latitude": py, "longitude": px, "horario_gps": t,
                             "coletado_em": fim_ciclo, "payload": {"p": int(p), "ta": t.isoformat(), "py": py, "px": px,
                                                                    "fonte": "202605 (Insper)"}})
                if len(lote) >= 5000:
                    db.session.execute(insert(RegistroBrutoGPS.__table__), lote)
                    lote = []
            if lote:
                db.session.execute(insert(RegistroBrutoGPS.__table__), lote)
            db.session.commit()
        relatorio["brutos"] = {"posicoes": RegistroBrutoGPS.query.count(), "ciclos": ExecucaoColeta.query.count(),
                               "linhas_olho_vivo": len(vinculos)}
        marcar("carga_brutos", t0)

        from app.viagens import cadeia

        t0 = relogio.perf_counter()
        execucoes = []
        agora = b + timedelta(minutes=40)  # depois do período: viagens terminadas
        cadeia.JANELA_RECONSTRUCAO = (b - a) + timedelta(minutes=40)
        while True:
            resultado = cadeia.processar_cadeia(agora)
            execucoes.append(resultado)
            if not resultado.get("correspondencia", {}).get("processados"):
                break
        relatorio["cadeia"] = {"execucoes": len(execucoes), "primeira": execucoes[0], "ultima": execucoes[-1]}
        marcar("cadeia", t0)

        O = ObservacaoTratada
        relatorio["correspondencia"] = dict(Counter(
            f"{'VALIDA' if v is True else 'INVALIDA' if v is False else 'PENDENTE'}:{m}"
            for v, m in db.session.query(O.valida, O.motivo_correspondencia)).most_common())
        viagens = ViagemObservada.query.all()
        coberturas = sorted(v.cobertura_tracado for v in viagens)
        relatorio["viagens"] = {
            "reconstruidas": len(viagens),
            "cobertura": {"p10": coberturas[len(coberturas) // 10], "mediana": coberturas[len(coberturas) // 2],
                          "p90": coberturas[len(coberturas) * 9 // 10],
                          "acima_de_80pct": sum(c >= 0.8 for c in coberturas)} if coberturas else None,
            "saida": dict(Counter(v.qualidade.get("saida") for v in viagens)),
            "diferenca_partida_min_mediana": sorted(abs(v.qualidade.get("diferenca_partida_seg", 0)) / 60
                                                     for v in viagens)[len(viagens) // 2] if viagens else None,
            "passagens": PassagemEstimada.query.count()}

        from app.indicadores.agenda import consolidar_dia

        t0 = relogio.perf_counter()
        execucao = consolidar_dia(args.dia, datetime.combine(args.dia + timedelta(days=1), time(5), FUSO))
        marcar("consolidacao", t0)
        I = IndicadorOperacional
        publicados = Counter(t for (t,) in db.session.query(I.indicador))
        com_valor = Counter(t for (t,) in db.session.query(I.indicador).filter(I.valor.isnot(None)))
        relatorio["indicadores"] = {"execucao": execucao.parametros.get("registros"),
                                    "falhas_de_calculo": len(execucao.parametros.get("falhas") or []),
                                    "publicados": dict(publicados), "com_valor": dict(com_valor),
                                    "amostra_cumprimento": [
                                        {"linha": r.linha_codigo, "sentido": r.sentido, "inicio": r.periodo_inicio.isoformat(),
                                         "valor": r.valor, "contagens": [r.contagem_numerador, r.contagem_denominador]}
                                        for r in I.query.filter(I.indicador == "CUMPRIMENTO_VIAGENS", I.valor.isnot(None))
                                        .order_by(I.periodo_inicio).limit(5)]}
    args.saida.parent.mkdir(parents=True, exist_ok=True)
    args.saida.write_text(json.dumps(relatorio, ensure_ascii=False, indent=1, default=str) + "\n", encoding="utf-8")
    print(json.dumps({k: relatorio[k] for k in ("tempos_s", "viagens", "indicadores")}, ensure_ascii=False, default=str)[:3000])


if __name__ == "__main__":
    main()
