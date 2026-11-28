"""US31-T02A: gerador de histórico SINTÉTICO em lotes, com semente fixa e relações válidas.

Gera, dia a dia, o que a coleta e o processamento gravariam (diagrama de classes, p. 2 e 3):

- ``ExecucaoColeta`` a cada ``intervalo_s``; ciclos dentro de uma ``LacunaColeta`` ficam FALHA,
  sem registros (a lacuna é o intervalo sem cobertura, não um registro inventado);
- ``RegistroBrutoGPS`` apontando ao ciclo que o persistiu: cada veículo tem posição nova em
  ``fracao_novos`` dos ciclos (coleta real: novos ÷ recebidos ≈ 0,70; o resto é a mesma posição
  repetida, que não é gravada de novo);
- ``ObservacaoTratada`` para cada bruto (versão de correspondência e GTFS; ``fracao_invalidas``
  marcadas inválidas com motivo);
- ``IndicadorOperacional`` (CUMPRIMENTO e PONTUALIDADE por linha, sentido e janela de 15 min).

Determinístico: a mesma semente e os mesmos parâmetros recriam exatamente os mesmos registros e
ids. Tudo é marcado como sintético (``payload.sintetico``, versões ``*-sintetico-us31``). Nunca usa
a base real: ``gerar`` exige ``permitir`` explícito para MySQL e recusa host não local.

Uso: ``python -m scripts.gerar_historico --dias 1 --veiculos 200 --linhas 20`` (base em
``US31_HISTORICO_MYSQL_URL``, MySQL local ``us31_*``), ou ``gerar(engine, Parametros(...))``.
"""
import argparse
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import random
import time
import uuid

from sqlalchemy import create_engine, insert, text

UTC = timezone.utc
NAMESPACE = uuid.UUID("3b0f6c4e-31a0-4d1e-9a35-000000000031")
VERSAO = "sintetico-us31-v1"
JANELA = timedelta(minutes=15)


@dataclass(frozen=True)
class Parametros:
    inicio: str = "2021-12-01T03:00:00+00:00"   # 00:00 em São Paulo
    dias: float = 1.0
    veiculos: int = 200                        # posições recebidas por ciclo
    linhas: int = 20
    intervalo_s: int = 30
    fracao_novos: float = 0.70                 # coleta real 26/11/2026: 7.483 novos ÷ 10.727 recebidos
    fracao_invalidas: float = 0.05
    lacunas_por_dia: int = 1
    duracao_lacuna_min: int = 10
    indicadores: bool = True
    semente: int = 31
    id_inicial_bruto: int = 1
    tabelas: tuple = field(default=("execucoes_coleta", "lacunas_coleta", "registros_gps", "observacoes_tratadas",
                                    "indicadores_operacionais"))


def _uid(*partes):
    return str(uuid.uuid5(NAMESPACE, ":".join(map(str, partes))))


def _naive(t):
    """Instante em UTC com fuso (o tipo ``UTCDateTime`` do modelo recusa horário sem fuso)."""
    return t.astimezone(UTC)


def linhas_sinteticas(n):
    return [f"8{n_:03d}-31" for n_ in range(n)]


def _frota(p, aleatorio):
    """Veículos por linha com a forma medida na coleta real (mediana baixa, poucas linhas cheias)."""
    linhas = linhas_sinteticas(p.linhas)
    pesos = [1 / (1 + i) ** 0.6 for i in range(p.linhas)]
    return [(f"7{v:05d}", aleatorio.choices(linhas, pesos)[0]) for v in range(p.veiculos)]


def _ciclos_do_dia(p, dia_inicio, dia_fim, aleatorio):
    total = int((dia_fim - dia_inicio).total_seconds() // p.intervalo_s)
    lacunas = []
    for k in range(p.lacunas_por_dia):
        inicio = dia_inicio + timedelta(seconds=aleatorio.randrange(0, total - 1) * p.intervalo_s)
        lacunas.append((_uid(p.semente, "lacuna", inicio.isoformat()), inicio,
                        inicio + timedelta(minutes=p.duracao_lacuna_min)))
    return total, lacunas


def gerar(engine, p=Parametros(), *, permitir=False, ao_dia=None):
    """Grava o lote em ``engine`` e devolve o manifesto (parâmetros, contagens e intervalo)."""
    if engine.dialect.name in ("mysql", "mariadb"):
        if not permitir or engine.url.host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("gerador só grava em MySQL local isolado, com permitir=True")
    from app.models import (
        ArquivoArmazenado, ExecucaoColeta, IndicadorOperacional, LacunaColeta, Linha, ObservacaoTratada,
        RegistroBrutoGPS, Veiculo, VersaoGTFS,
    )

    aleatorio = random.Random(p.semente)
    inicio = datetime.fromisoformat(p.inicio).astimezone(UTC)
    fim = inicio + timedelta(days=p.dias)
    gtfs_id, arquivo_id = _uid(p.semente, "gtfs"), _uid(p.semente, "arquivo")
    frota = _frota(p, aleatorio)
    contagens = dict.fromkeys(("versoes_gtfs", "linhas", "veiculos", "execucoes_coleta", "lacunas_coleta",
                               "registros_gps", "observacoes_tratadas", "indicadores_operacionais"), 0)
    t0 = time.perf_counter()
    with engine.begin() as c:
        c.execute(insert(ArquivoArmazenado.__table__), [{
            "id": arquivo_id, "chave": f"gtfs/historico-us31-{p.semente}.zip", "nome_original": "historico-us31.zip",
            "mime_type": "application/zip", "tamanho_bytes": 0, "sha256": hashlib.sha256(gtfs_id.encode()).hexdigest(),
            "criado_em": _naive(inicio)}])
        c.execute(insert(VersaoGTFS.__table__), [{
            "id": gtfs_id, "capturada_em": _naive(inicio), "hash": hashlib.sha256(arquivo_id.encode()).hexdigest(),
            "vigencia_inicio": _naive(inicio).date(), "vigencia_fim": _naive(fim).date(), "arquivo_id": arquivo_id}])
        c.execute(insert(Linha.__table__), [{"versao_gtfs_id": gtfs_id, "codigo": l, "nome": f"Linha sintética {l}"}
                                            for l in linhas_sinteticas(p.linhas)])
        c.execute(insert(Veiculo.__table__), [{"prefixo": v} for v, _ in frota])
    contagens.update(versoes_gtfs=1, linhas=p.linhas, veiculos=len(frota))

    proximo_bruto, proximo_ciclo = p.id_inicial_bruto, None
    with engine.connect() as c:
        proximo_ciclo = (c.execute(text("SELECT COALESCE(MAX(id), 0) FROM execucoes_coleta")).scalar() or 0) + 1
    dia = inicio
    while dia < fim:
        fim_dia = min(dia + timedelta(days=1), fim)
        total, lacunas = _ciclos_do_dia(p, dia, fim_dia, aleatorio)
        ciclos, brutos, tratadas, gaps = [], [], [], []
        for lacuna_id, a, b in lacunas:
            if a < fim_dia:
                gaps.append({"id": lacuna_id, "inicio": _naive(a), "fim": _naive(min(b, fim_dia)),
                             "causa": "SPTRANS_HTTP_503", "fonte": "OLHO_VIVO"})
        n = 0
        t = dia
        while t < fim_dia:
            ciclo_id = proximo_ciclo
            proximo_ciclo += 1
            em_lacuna = next((g["id"] for g in gaps if g["inicio"] <= _naive(t) < g["fim"]), None)
            novos = 0
            if em_lacuna is None:
                for prefixo, linha in frota:
                    if aleatorio.random() >= p.fracao_novos:
                        continue  # mesma posição do ciclo anterior: não é gravada de novo
                    horario = t - timedelta(seconds=aleatorio.randint(1, 25))
                    lat, lon = -23.55 + aleatorio.uniform(-0.2, 0.2), -46.63 + aleatorio.uniform(-0.2, 0.2)
                    sentido = aleatorio.choice(("1", "2"))
                    brutos.append({"id": proximo_bruto, "execucao_id": ciclo_id, "veiculo_prefixo": prefixo,
                                   "prefixo_origem": prefixo, "linha_origem": linha.split("-")[0],
                                   "sentido_origem": sentido, "latitude": lat, "longitude": lon,
                                   "horario_gps": _naive(horario), "coletado_em": _naive(t),
                                   "payload": {"p": prefixo, "a": True, "ta": horario.isoformat(), "py": lat, "px": lon,
                                               "sintetico": "US31"}})
                    invalida = aleatorio.random() < p.fracao_invalidas
                    tratadas.append({"id": _uid(p.semente, "tratada", proximo_bruto), "bruto_id": proximo_bruto,
                                     "valida": not invalida, "motivo_invalidez": "FORA_DO_TRACADO" if invalida else None,
                                     "versao_correspondencia": VERSAO, "versao_gtfs_id": gtfs_id,
                                     "linha_codigo": linha, "sentido_gtfs": "0" if sentido == "1" else "1",
                                     "versao_validacao": VERSAO, "processada_em": _naive(t + timedelta(seconds=40))})
                    proximo_bruto += 1
                    novos += 1
            ciclos.append({"id": ciclo_id, "fonte": "OLHO_VIVO", "prevista_em": _naive(t), "iniciada_em": _naive(t),
                           "finalizada_em": _naive(t + timedelta(seconds=3)),
                           "estado": "FALHA" if em_lacuna else "OK", "tentativas": 4 if em_lacuna else 1,
                           "recebidos": 0 if em_lacuna else len(frota), "novos_persistidos": novos,
                           "erro": "SPTRANS_HTTP_503" if em_lacuna else None, "lacuna_id": em_lacuna,
                           "confirmada_em": _naive(t + timedelta(seconds=3))})
            n += 1
            t += timedelta(seconds=p.intervalo_s)
        indicadores = []
        if p.indicadores:
            w = dia
            while w < fim_dia:
                for linha in linhas_sinteticas(p.linhas):
                    for sentido in ("0", "1"):
                        for tipo in ("CUMPRIMENTO_VIAGENS", "PONTUALIDADE_VIAGENS"):
                            den = aleatorio.randint(1, 8)
                            num = aleatorio.randint(0, den)
                            indicadores.append({
                                "id": _uid(p.semente, "ind", linha, sentido, tipo, w.isoformat()),
                                "chave_resultado": hashlib.sha256(f"{p.semente}{linha}{sentido}{tipo}{w}".encode()).hexdigest()[:64],
                                "versao_gtfs_id": gtfs_id, "linha_codigo": linha, "sentido": sentido, "indicador": tipo,
                                "valor": 100.0 * num / den, "unidade": "%", "periodo_inicio": _naive(w),
                                "periodo_fim": _naive(w + JANELA), "contagem_numerador": num,
                                "contagem_denominador": den, "versao_conjunto": f"historico-{VERSAO}",
                                "versao_correspondencia": VERSAO, "versao_calculo": f"{tipo.lower()}-{VERSAO}",
                                "qualidade": {"origem": "SINTETICA_US31"}, "publicado_em": _naive(w + JANELA + timedelta(minutes=1))})
                w += JANELA
        with engine.begin() as c:  # um dia por transação: o lote interrompido não deixa dia pela metade
            if gaps:
                c.execute(insert(LacunaColeta.__table__), gaps)
            for k in range(0, len(ciclos), 5000):
                c.execute(insert(ExecucaoColeta.__table__), ciclos[k:k + 5000])
            for k in range(0, len(brutos), 5000):
                c.execute(insert(RegistroBrutoGPS.__table__), brutos[k:k + 5000])
                c.execute(insert(ObservacaoTratada.__table__), tratadas[k:k + 5000])
            for k in range(0, len(indicadores), 5000):
                c.execute(insert(IndicadorOperacional.__table__), indicadores[k:k + 5000])
        contagens["execucoes_coleta"] += len(ciclos)
        contagens["lacunas_coleta"] += len(gaps)
        contagens["registros_gps"] += len(brutos)
        contagens["observacoes_tratadas"] += len(tratadas)
        contagens["indicadores_operacionais"] += len(indicadores)
        if ao_dia:
            ao_dia(dia, contagens)
        dia = fim_dia
    parametros = {k: v for k, v in asdict(p).items() if k != "tabelas"}
    return {"gerador": "scripts/gerar_historico.py", "versao": VERSAO, "sintetico": True, "parametros": parametros,
            "hash_parametros": hashlib.sha256(json.dumps(parametros, sort_keys=True).encode()).hexdigest(),
            "intervalo_utc": [inicio.isoformat(), fim.isoformat()], "contagens": contagens,
            "ids_brutos": [p.id_inicial_bruto, proximo_bruto - 1], "segundos": round(time.perf_counter() - t0, 1),
            "registros_por_segundo": round(contagens["registros_gps"] / max(time.perf_counter() - t0, 1e-9))}


def conferir(engine, manifesto):
    """US31-T02B: confere a base contra o manifesto — intervalo, contagens, relações e marcação de
    dado sintético. Cada item traz o esperado, o encontrado e ``ok``; só leitura."""
    # Comparação em UTC sem fuso: o MySQL devolve DATETIME sem fuso e o SQLite, texto.
    inicio, fim = (datetime.fromisoformat(t).astimezone(UTC).replace(tzinfo=None) for t in manifesto["intervalo_utc"])
    itens = {}

    def item(nome, esperado, encontrado):
        itens[nome] = {"esperado": esperado, "encontrado": encontrado, "ok": esperado == encontrado}

    with engine.connect() as c:
        um = lambda sql: c.execute(text(sql)).scalar()  # noqa: E731
        for tabela, n in manifesto["contagens"].items():
            item(f"contagem.{tabela}", n, um(f"SELECT COUNT(*) FROM {tabela}"))
        def dt(v):
            v = datetime.fromisoformat(v) if isinstance(v, str) else v
            return v.astimezone(UTC).replace(tzinfo=None) if v is not None and v.tzinfo else v

        de, ate = map(dt, c.execute(text("SELECT MIN(prevista_em), MAX(prevista_em) FROM execucoes_coleta")).one())
        item("intervalo.ciclos_dentro_do_manifesto", True, bool(de and ate and inicio <= de and ate < fim))
        de, ate = map(dt, c.execute(text("SELECT MIN(periodo_inicio), MAX(periodo_fim) FROM indicadores_operacionais")).one())
        item("intervalo.indicadores_dentro_do_manifesto", True, bool(de and ate and inicio <= de and ate <= fim))
        item("relacao.bruto_sem_ciclo", 0, um("SELECT COUNT(*) FROM registros_gps r LEFT JOIN execucoes_coleta e "
                                               "ON e.id = r.execucao_id WHERE e.id IS NULL"))
        item("relacao.bruto_em_ciclo_de_lacuna", 0, um("SELECT COUNT(*) FROM registros_gps r JOIN execucoes_coleta e "
                                                        "ON e.id = r.execucao_id WHERE e.lacuna_id IS NOT NULL"))
        item("relacao.tratada_sem_bruto", 0, um("SELECT COUNT(*) FROM observacoes_tratadas o LEFT JOIN registros_gps r "
                                                 "ON r.id = o.bruto_id WHERE r.id IS NULL"))
        item("relacao.bruto_sem_tratada", 0, um("SELECT COUNT(*) FROM registros_gps r LEFT JOIN observacoes_tratadas o "
                                                 "ON o.bruto_id = r.id WHERE o.id IS NULL"))
        item("relacao.indicador_de_linha_inexistente", 0, um(
            "SELECT COUNT(*) FROM indicadores_operacionais i LEFT JOIN linhas l ON l.codigo = i.linha_codigo "
            "AND l.versao_gtfs_id = i.versao_gtfs_id WHERE l.codigo IS NULL"))
        item("sintetico.tratadas_fora_da_versao", 0, um(
            f"SELECT COUNT(*) FROM observacoes_tratadas WHERE versao_correspondencia <> '{VERSAO}'"))
        item("sintetico.indicadores_fora_do_conjunto", 0, um(
            f"SELECT COUNT(*) FROM indicadores_operacionais WHERE versao_conjunto <> 'historico-{VERSAO}'"))
        item("sintetico.linhas_sem_marca", 0, um("SELECT COUNT(*) FROM linhas WHERE nome NOT LIKE 'Linha sint%'"))
        item("ids_brutos", manifesto["ids_brutos"], list(c.execute(text("SELECT MIN(id), MAX(id) FROM registros_gps")).one()))
    return {"ok": all(i["ok"] for i in itens.values()), "itens": itens}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for nome, tipo in (("dias", float), ("veiculos", int), ("linhas", int), ("semente", int), ("lacunas_por_dia", int)):
        parser.add_argument(f"--{nome.replace('_', '-')}", type=tipo, default=getattr(Parametros, nome))
    parser.add_argument("--inicio", default=Parametros.inicio)
    parser.add_argument("--manifesto", default=None, help="Arquivo JSON do manifesto")
    parser.add_argument("--conferir", default=None, metavar="MANIFESTO",
                        help="Só confere a base contra o manifesto (T02B) e grava a conferência nele")
    args = parser.parse_args(argv)
    url = os.getenv("US31_HISTORICO_MYSQL_URL")
    if not url or "/us31_" not in url:
        raise SystemExit("Defina US31_HISTORICO_MYSQL_URL (MySQL local, base us31_*).")
    from app import create_app, db
    from flask_migrate import upgrade

    longo = {"connect_args": {"read_timeout": 3600, "write_timeout": 3600}}
    app = create_app({"SQLALCHEMY_DATABASE_URI": url, "MYSQL_SSL_CA": None, "SQLALCHEMY_ENGINE_OPTIONS": longo})
    if args.conferir:
        with open(args.conferir, encoding="utf-8") as arquivo:
            manifesto = json.load(arquivo)
        t0 = time.perf_counter()
        with app.app_context():
            conferencia = conferir(db.engine, manifesto)
        manifesto["conferencia"] = {"executada_em": datetime.now(UTC).isoformat(),
                                    "segundos": round(time.perf_counter() - t0, 1), **conferencia}
        with open(args.conferir, "w", encoding="utf-8") as arquivo:
            arquivo.write(json.dumps(manifesto, ensure_ascii=False, indent=1) + "\n")
        print(json.dumps({"ok": conferencia["ok"], "divergentes": [k for k, v in conferencia["itens"].items()
                                                                   if not v["ok"]]}, ensure_ascii=False))
        return 0 if conferencia["ok"] else 1
    with app.app_context():
        upgrade()
        p = Parametros(inicio=args.inicio, dias=args.dias, veiculos=args.veiculos, linhas=args.linhas,
                       semente=args.semente, lacunas_por_dia=args.lacunas_por_dia)
        manifesto = gerar(db.engine, p, permitir=True,
                          ao_dia=lambda d, c: print(f"{d.date()} registros_gps={c['registros_gps']}", flush=True))
    texto = json.dumps(manifesto, ensure_ascii=False, indent=1)
    if args.manifesto:
        with open(args.manifesto, "w", encoding="utf-8") as arquivo:
            arquivo.write(texto + "\n")
    print(texto)


if __name__ == "__main__":
    raise SystemExit(main())
