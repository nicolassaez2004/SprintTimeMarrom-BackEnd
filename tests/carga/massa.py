"""US27-T01: massa SINTÉTICA de 12 meses de indicadores numa base MySQL ISOLADA.

Reproduz o que a agenda (US23-T03) publica para cada linha e sentido: CUMPRIMENTO_VIAGENS e
PONTUALIDADE_VIAGENS a cada janela de 15 min (nível da linha) e, em algumas linhas, os
indicadores por parada (frequência, regularidade, espera e excesso). Os ciclos de coleta
(``execucoes_coleta``, um a cada 30 s) cobrem os mesmos 12 meses, porque /series os lê.
Semente fixa: a mesma chamada gera exatamente os mesmos registros. Tudo é marcado como
sintético (``versao_conjunto`` e ``qualidade.origem``).
"""
from datetime import datetime, timedelta, timezone
import json
import random
import time

UTC = timezone.utc
GTFS_ID = "00000000-0000-0000-0000-0000000027a0"
ARQUIVO_ID = "00000000-0000-0000-0000-0000000027a1"
CONJUNTO = "carga-us27-sintetico-v1"
JANELA = timedelta(minutes=15)
POR_LINHA = (("CUMPRIMENTO_VIAGENS", "%"), ("PONTUALIDADE_VIAGENS", "%"))
POR_PARADA = (("FREQUENCIA_OBSERVADA", "viagens/h"), ("FREQUENCIA_PROGRAMADA", "viagens/h"),
              ("REGULARIDADE_INTERVALOS", "cv"), ("ESPERA_ESTIMADA", "min"), ("EXCESSO_ESPERA", "min"))
INDICADORES = tuple(t for t, _ in POR_LINHA + POR_PARADA)


def linhas(quantidade):
    return [f"9{n:03d}-27" for n in range(quantidade)]


def paradas(linha, quantidade):
    return [f"{linha[:4]}P{n}" for n in range(quantidade)]


def _naive(t):
    return t.astimezone(UTC).replace(tzinfo=None)


def _falhas_coleta(linha, inicio, fim):
    return {"versao_regra": "falhas-coleta-1", "escopo": "COLETA_GLOBAL", "fonte": "OLHO_VIVO",
            "linha_exibida": linha, "inicio": inicio.isoformat(), "fim": fim.isoformat(),
            "intervalo_coleta_segundos": 30, "janelas_15min": 1, "janelas_sem_expectativa": 0,
            "ciclos_esperados": 30, "ciclos_registrados": 30, "ciclos_com_dados": 30, "ciclos_falha": 0,
            "ciclos_em_andamento": 0, "falhas_da_fonte": 0, "sem_registro": 0, "falhas": 0, "denominador": 30,
            "percentual_falhas": 0.0, "situacao": "COMPLETA", "motivo": None, "lacunas": []}


def carregar(conexao, inicio, fim, *, n_linhas=20, linhas_com_parada=4, paradas_por_linha=3, semente=27):
    """Grava a massa em [inicio, fim) usando a conexão PyMySQL (autocommit). Idempotente."""
    aleatorio = random.Random(semente)
    t0 = time.perf_counter()
    with conexao.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM indicadores_operacionais WHERE versao_conjunto=%s", (CONJUNTO,))
        if cur.fetchone()[0]:
            return {"reaproveitada": True}
        agora = datetime.now(UTC).replace(tzinfo=None)
        cur.execute("INSERT INTO arquivos_armazenados (id, chave, nome_original, mime_type, tamanho_bytes, sha256, "
                    "criado_em) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (ARQUIVO_ID, "gtfs/carga-us27.zip", "carga-us27.zip", "application/zip", 0, "c" * 64, agora))
        cur.execute("INSERT INTO versoes_gtfs (id, capturada_em, hash, vigencia_inicio, vigencia_fim, arquivo_id) "
                    "VALUES (%s,%s,%s,%s,%s,%s)", (GTFS_ID, agora, "c" * 64, _naive(inicio).date(),
                                                    _naive(fim).date(), ARQUIVO_ID))
        codigos = linhas(n_linhas)
        cur.executemany("INSERT INTO linhas (versao_gtfs_id, codigo, nome) VALUES (%s,%s,%s)",
                        [(GTFS_ID, c, f"Linha sintética {c}") for c in codigos])
        com_parada = codigos[:linhas_com_parada]
        todas_paradas = [(GTFS_ID, p, f"Parada sintética {p}", -23.5 - i * 0.001, -46.6 - i * 0.001)
                         for i, p in enumerate(p for c in com_parada for p in paradas(c, paradas_por_linha))]
        cur.executemany("INSERT INTO paradas (versao_gtfs_id, codigo, nome, latitude, longitude) "
                        "VALUES (%s,%s,%s,%s,%s)", todas_paradas)

        # Ciclos de coleta: um a cada 30 s em todo o período.
        ciclos, n_ciclos, t = [], 0, inicio
        while t < fim:
            c = _naive(t)
            ciclos.append(("OLHO_VIVO", c, c, c + timedelta(seconds=2), "OK", 1, 11000, 7500,
                           c + timedelta(seconds=2)))
            t += timedelta(seconds=30)
            if len(ciclos) >= 20000:
                cur.executemany("INSERT INTO execucoes_coleta (fonte, prevista_em, iniciada_em, finalizada_em, "
                                "estado, tentativas, recebidos, novos_persistidos, confirmada_em) "
                                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)", ciclos)
                n_ciclos += len(ciclos)
                ciclos = []
        if ciclos:
            cur.executemany("INSERT INTO execucoes_coleta (fonte, prevista_em, iniciada_em, finalizada_em, estado, "
                            "tentativas, recebidos, novos_persistidos, confirmada_em) "
                            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)", ciclos)
            n_ciclos += len(ciclos)

        sql = ("INSERT INTO indicadores_operacionais (id, chave_resultado, versao_gtfs_id, linha_codigo, sentido, "
               "parada_codigo, indicador, valor, unidade, periodo_inicio, periodo_fim, contagem_numerador, "
               "contagem_denominador, versao_conjunto, versao_correspondencia, versao_calculo, qualidade, "
               "publicado_em) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)")
        lote, total, seq = [], 0, 0

        def gravar():
            nonlocal lote, total
            if lote:
                cur.executemany(sql, lote)
                total += len(lote)
                lote = []

        t = inicio
        while t < fim:
            a, b = t, t + JANELA
            qualidade_cache = {}
            for linha in codigos:
                if linha not in qualidade_cache:
                    qualidade_cache[linha] = json.dumps({"origem": "SINTETICA_US27", "falhas_coleta":
                                                         _falhas_coleta(linha, a, b)}, separators=(",", ":"))
                for sentido in ("0", "1"):
                    alvos = [(None, POR_LINHA)]
                    if linha in com_parada:
                        alvos += [(p, POR_PARADA) for p in paradas(linha, paradas_por_linha)]
                    for parada, tipos in alvos:
                        for indicador, unidade in tipos:
                            seq += 1
                            den = aleatorio.randint(2, 8) if unidade == "%" else None
                            num = aleatorio.randint(0, den) if den else None
                            valor = (100.0 * num / den) if den else round(aleatorio.uniform(0.5, 12), 3)
                            lote.append((f"c27-{seq:011d}", f"carga-us27-{seq:011d}", GTFS_ID, linha, sentido, parada,
                                         indicador, valor, unidade, _naive(a), _naive(b), num, den, CONJUNTO,
                                         "match-carga-1", f"{indicador.lower()}-carga-1",
                                         qualidade_cache[linha], _naive(b) + timedelta(minutes=1)))
            if len(lote) >= 20000:
                gravar()
            t = b
        gravar()
    return {"reaproveitada": False, "semente": semente, "linhas": n_linhas, "linhas_com_paradas": linhas_com_parada,
            "paradas_por_linha": paradas_por_linha, "ciclos_coleta": n_ciclos, "indicadores": total,
            "segundos_carga": round(time.perf_counter() - t0, 1)}
