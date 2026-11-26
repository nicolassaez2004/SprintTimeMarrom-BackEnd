"""Banco DESCARTÁVEL de demonstração das Sprints 1 e 2 (validação do PO).

Cria um SQLite novo numa pasta vazia e executa o pipeline real a partir de
viagens observadas SINTÉTICAS: passagens (US15-T01) → agenda de indicadores
(US14/15/16/17/18, US23-T03) → CSV (US10) e Parquet (US09). A rede é sintética;
os limites territoriais são o snapshot oficial GeoSampa versionado no repositório.

Nunca conecta à Aiven nem à SPTrans. Tudo é marcado como SINTÉTICO.

    python -m scripts.demo_sprints_1_2 --pasta /tmp/demo-marrom            # prepara
    python -m scripts.demo_sprints_1_2 --pasta /tmp/demo-marrom --servir   # serve em :5000

Limitação declarada: a reconstrução de viagens a partir do GPS (US14-T01C) não
existe no código; por isso as ``ViagemObservada`` são inseridas por este script.
"""

import argparse
from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
import os
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

os.environ["DATABASE_URL"] = "sqlite://"  # nunca o .env: o app é criado com o SQLite abaixo
os.environ.setdefault("SPTRANS_TOKEN", "")

from pyproj import Transformer  # noqa: E402
from sqlalchemy import event, insert  # noqa: E402

from app import create_app, db  # noqa: E402
from app.documentos.service import catalogar_documento  # noqa: E402
from app.exportacoes.csv import publicar_csv_indicadores  # noqa: E402
from app.exportacoes.parquet import publicar_brutos  # noqa: E402
from app.indicadores.agenda import consolidar_dia  # noqa: E402
from app.models import (  # noqa: E402
    ArquivoArmazenado, ExecucaoColeta, ImportacaoProgramacao, LacunaColeta, Linha,
    ObservacaoTratada, Parada, PartidaProgramada, RegistroBrutoGPS, Trecho, Veiculo,
    VersaoGTFS, ViagemObservada,
)
from app.territorios.associacao import associar_rede_a_territorios  # noqa: E402
from app.territorios.importacao import importar_snapshot_geosampa  # noqa: E402
from app.viagens.passagens import estimar_e_gravar  # noqa: E402

UTC, FUSO = timezone.utc, ZoneInfo("America/Sao_Paulo")
DIA = date(2026, 11, 24)
LINHA = "DEMO-01"
RECON, MATCH = "DEMO-SINTETICA-recon", "DEMO-SINTETICA-match"
ORIGEM = "SINTETICA_DEMONSTRACAO"
CONSOLIDADO_EM = datetime(2026, 11, 25, 8, 30, tzinfo=UTC)  # 05:30 em São Paulo, antes do prazo
PARADAS_M = {"A": 0, "B": 700, "C": 1400, "D": 2100, "E": 2800}  # metros a leste da Praça da Sé
MIN_POR_TRECHO = 3
uid = lambda nome: str(uuid5(NAMESPACE_URL, f"demo-sprints-1-2:{nome}"))  # noqa: E731


def local(h, m=0, s=0, dia=DIA):
    return (datetime.combine(dia, time(h), FUSO) + timedelta(minutes=m, seconds=s)).astimezone(UTC)


def criar_app(pasta):
    return create_app({
        "SQLALCHEMY_DATABASE_URI": "sqlite:///" + (pasta / "demo.sqlite").as_posix(),
        "MYSQL_SSL_CA": None, "SPTRANS_TOKEN": None, "PASSAGEM_LACUNA_MAXIMA_SEG": "120",
        "EXPORTACOES_DIR": str(pasta / "exportacoes"), "DOCUMENTOS_STORAGE_DIR": str(pasta / "documentos"),
        "GTFS_STORAGE_DIR": str(pasta / "gtfs"), "PROGRAMACAO_STORAGE_DIR": str(pasta / "programacao"),
    })


def _rede():
    para_m = Transformer.from_crs(4326, 31983, always_xy=True)
    para_g = Transformer.from_crs(31983, 4326, always_xy=True)
    x0, y0 = para_m.transform(-46.6339, -23.5503)
    ponto = lambda m: para_g.transform(x0 + m, y0)  # noqa: E731
    arquivo = ArquivoArmazenado(id=uid("arquivo"), chave="demo/rede.json", nome_original="rede-demo.json",
                                mime_type="application/json", tamanho_bytes=0,
                                sha256=sha256(b"demo-sprints-1-2").hexdigest(), criado_em=CONSOLIDADO_EM)
    versao = VersaoGTFS(id=uid("gtfs"), hash=arquivo.sha256, arquivo=arquivo, capturada_em=CONSOLIDADO_EM,
                        vigencia_inicio=date(2026, 1, 1), vigencia_fim=date(2026, 12, 31))
    db.session.add_all([versao, Linha(versao_gtfs=versao, codigo=LINHA, nome="Linha DEMONSTRAÇÃO (sintética)")])
    for codigo, m in PARADAS_M.items():
        lon, lat = ponto(m)
        db.session.add(Parada(versao_gtfs_id=versao.id, codigo=codigo, nome=f"Parada demo {codigo}",
                              longitude=lon, latitude=lat))
    db.session.flush()
    for sentido, seq in (("0", "ABCDE"), ("1", "EDCBA")):
        for ordem, (a, b) in enumerate(zip(seq, seq[1:]), 1):
            db.session.add(Trecho(id=uid(f"trecho:{sentido}:{ordem}"), versao_gtfs_id=versao.id,
                                  linha_codigo=LINHA, sentido=sentido, trajeto=f"demo-{sentido}", ordem=ordem,
                                  parada_origem_codigo=a, parada_destino_codigo=b,
                                  geometria=[list(ponto(PARADAS_M[a])), list(ponto(PARADAS_M[b]))]))
    db.session.commit()
    return versao, arquivo, ponto


def _programacao(versao, arquivo):
    """Ida (0): horário FIXO a cada 15 min; volta (1): FREQUÊNCIA de 10 min. 06:00–09:59."""
    grades = {}
    for sentido, tipo, passo in (("0", "FIXA", 15), ("1", "FREQUENCIA", 10)):
        lote = ImportacaoProgramacao(id=uid(f"lote:{sentido}"), versao_gtfs_id=versao.id, linha_codigo=LINHA,
                                     data_operacao=DIA, sentido=sentido, tipo_dia="UTIL", fonte="GTFS",
                                     versao_fonte=f"demo-{sentido}", fonte_url="urn:demo:programacao-sintetica",
                                     capturada_em=CONSOLIDADO_EM, verificada_em=CONSOLIDADO_EM, arquivo=arquivo)
        partidas = [PartidaProgramada(id=uid(f"partida:{sentido}:{i}"), importacao=lote, ordem=i + 1,
                                      saida_prevista=local(6) + timedelta(minutes=passo * i), tipo_oferta=tipo,
                                      intervalo_seg=passo * 60 if tipo == "FREQUENCIA" else None)
                    for i in range(240 // passo)]
        db.session.add(lote)
        db.session.add_all(partidas)
        grades[sentido] = partidas
    db.session.commit()
    return grades


# Falhas de coleta (US26): 06:30 com 5/30 ciclos em falha (16,7% → aviso), 07:00 com
# exatamente 3/30 (10% → sem aviso) e 08:00–08:10 sem nenhum ciclo (lacuna registrada).
FALHAS = {**{local(6, 30, 30 * k): "SPTRANS_HTTP_503" for k in range(5)},
          **{local(7, 0, 30 * k): "SPTRANS_HTTP_503" for k in range(3)}}
SEM_CICLO = (local(8), local(8, 10))


def _coleta():
    ciclos, t = [], local(0)
    while t < local(0, dia=DIA + timedelta(days=1)):
        if not SEM_CICLO[0] <= t < SEM_CICLO[1]:
            ciclos.append({"id": len(ciclos) + 1, "fonte": "OLHO_VIVO", "prevista_em": t, "iniciada_em": t,
                           "finalizada_em": t + timedelta(seconds=2), "estado": "FALHA" if t in FALHAS else "OK",
                           "erro": FALHAS.get(t), "recebidos": 0 if t in FALHAS else 1, "novos_persistidos": 0})
        t += timedelta(seconds=30)
    db.session.execute(insert(ExecucaoColeta), ciclos)
    db.session.add(LacunaColeta(id=uid("lacuna"), inicio=SEM_CICLO[0], fim=SEM_CICLO[1],
                                causa="COLETOR_PARADO_DEMO", fonte="OLHO_VIVO"))
    db.session.commit()
    return {c["iniciada_em"]: c["id"] for c in ciclos if c["estado"] == "OK"}


# (índice da partida, atraso s, cobertura) — índices ausentes não têm viagem (não cumpridas).
IDA = [(0, 30, 1), (1, -90, 1), (2, 120, 1), (3, 400, 1), (4, 0, 1), (5, 60, 0.7), (6, 250, 1),
       (7, -30, 1), (8, 700, 1), (10, 45, 1), (11, 310, 1), (12, -61, 1), (13, 15, 1), (15, 5, 1)]
# Volta por frequência: saídas observadas (min após 06:00), com ônibus agrupados às 08:30.
VOLTA = [0, 11, 19, 31, 40, 52, 60, 70, 80, 90, 101, 110, 121, 130, 140, 150, 152, 154,
         180, 190, 200, 210, 221, 230]


def _viagens(grades, ponto, ciclos):
    seq = [("0", grades["0"][i], grades["0"][i].saida_prevista + timedelta(seconds=a), c) for i, a, c in IDA]
    for n, minuto in enumerate(VOLTA):
        partida = min(grades["1"], key=lambda p: abs((p.saida_prevista - local(6, minuto)).total_seconds()))
        if any(p is partida for _, p, _, _ in seq):
            partida = next(p for p in grades["1"] if not any(p is q for _, q, _, _ in seq))
        seq.append(("1", partida, local(6, minuto), 1))
    duracao = timedelta(minutes=MIN_POR_TRECHO * (len(PARADAS_M) - 1))
    for n, (sentido, partida, saida, cobertura) in enumerate(seq, 1):
        prefixo = f"DEMO{n:03d}"
        db.session.add(Veiculo(prefixo=prefixo))
        viagem = ViagemObservada(id=uid(f"viagem:{n}"), partida_programada=partida, saida_observada=saida,
                                 fim_observado=saida + duracao, cobertura_tracado=cobertura,
                                 versao_reconstrucao=RECON, versao_correspondencia=MATCH, qualidade={"origem": ORIGEM})
        db.session.add(viagem)
        observacoes, t = [], saida
        while t <= saida + duracao:
            ciclo = t.replace(second=t.second - t.second % 30, microsecond=0) + timedelta(seconds=30)
            if ciclo in ciclos:  # sem ciclo com sucesso não há posição gravada
                metros = PARADAS_M["E"] * (t - saida) / duracao
                lon, lat = ponto(metros if sentido == "0" else PARADAS_M["E"] - metros)
                bruto = RegistroBrutoGPS(execucao_id=ciclos[ciclo], veiculo_prefixo=prefixo, prefixo_origem=prefixo,
                                         linha_origem="99901", sentido_origem=str(int(sentido) + 1),
                                         latitude=lat, longitude=lon, horario_gps=t, coletado_em=ciclo,
                                         payload={"origem": ORIGEM, "p": prefixo})
                observacoes.append(ObservacaoTratada(bruto=bruto, valida=True, versao_gtfs_id=partida.importacao.versao_gtfs_id,
                                                     linha_codigo=LINHA, sentido_gtfs=sentido,
                                                     versao_correspondencia=MATCH))
                db.session.add_all([bruto, observacoes[-1]])
            t += timedelta(seconds=30)
        db.session.flush()
        estimar_e_gravar(viagem, observacoes, lacuna_maxima=timedelta(seconds=120))
    db.session.commit()
    return len(seq)


def _mapa_ao_vivo(versao, ponto):
    """Dois ciclos agora: DEMO-LIVE-2 tem o GPS inalterado e só existe no ciclo anterior."""
    agora = datetime.now(UTC).replace(microsecond=0)
    for i, (instante, veiculos) in enumerate(((agora - timedelta(seconds=30), ("DEMO-LIVE-1", "DEMO-LIVE-2")),
                                               (agora, ("DEMO-LIVE-1",)))):
        ciclo = ExecucaoColeta(fonte="OLHO_VIVO", prevista_em=instante, iniciada_em=instante, estado="OK",
                               finalizada_em=instante, recebidos=2, novos_persistidos=len(veiculos))
        db.session.add(ciclo)
        db.session.flush()
        for prefixo in veiculos:
            if db.session.get(Veiculo, prefixo) is None:
                db.session.add(Veiculo(prefixo=prefixo))
            lon, lat = ponto(400 * (i + 1) if prefixo.endswith("1") else 1800)
            bruto = RegistroBrutoGPS(execucao_id=ciclo.id, veiculo_prefixo=prefixo, prefixo_origem=prefixo,
                                     linha_origem="99901", sentido_origem="1", latitude=lat, longitude=lon,
                                     horario_gps=instante - timedelta(seconds=5), coletado_em=instante,
                                     payload={"origem": ORIGEM, "p": prefixo})
            db.session.add_all([bruto, ObservacaoTratada(bruto=bruto, valida=True, versao_gtfs_id=versao.id,
                                                         linha_codigo=LINHA, sentido_gtfs="0",
                                                         versao_correspondencia=MATCH)])
    db.session.commit()


def _documento(pasta):
    pdf = pasta / "documento-sintetico.pdf"
    pdf.write_bytes(b"%PDF-1.4\n% DOCUMENTO SINTETICO DE DEMONSTRACAO - sem valor oficial\n%%EOF\n")
    catalogar_documento(pdf, titulo="Documento SINTÉTICO de demonstração", tipo="CONTRATO",
                        data_documento=date(2026, 1, 15), orgao="Demonstração (sem valor oficial)",
                        fonte="Gerado por scripts.demo_sprints_1_2", descoberto_em=CONSOLIDADO_EM,
                        agora=CONSOLIDADO_EM)


def preparar(pasta):
    pasta.mkdir(parents=True, exist_ok=True)
    if any(pasta.iterdir()):
        raise SystemExit(f"A pasta {pasta} precisa estar vazia (banco descartável novo).")
    app = criar_app(pasta)
    with app.app_context():
        assert db.engine.dialect.name == "sqlite", "a demonstração só usa SQLite local"
        event.listen(db.engine, "connect", lambda c, _: c.execute("PRAGMA foreign_keys=ON"))
        db.create_all()
        versao, arquivo, ponto = _rede()
        territorio = importar_snapshot_geosampa()
        associar_rede_a_territorios(versao.id, territorio.id)
        grades = _programacao(versao, arquivo)
        ciclos = _coleta()
        viagens = _viagens(grades, ponto, ciclos)
        execucao = consolidar_dia(DIA, CONSOLIDADO_EM)
        recibo_csv = publicar_csv_indicadores(DIA, CONSOLIDADO_EM)
        parquet = publicar_brutos(CONSOLIDADO_EM, relogio=lambda: CONSOLIDADO_EM)
        _documento(pasta)
        _mapa_ao_vivo(versao, ponto)
        print(f"Demonstração SINTÉTICA pronta em {pasta}")
        print(f"  dia {DIA}, linha {LINHA}: {viagens} viagens; consolidação {execucao.estado} "
              f"({execucao.parametros['registros']} registros, falhas={len(execucao.parametros['falhas'])})")
        print(f"  CSV: {recibo_csv['registros']} linhas; Parquet: {parquet and parquet.get('registros')} registros")
        print(f"  versao_gtfs_id={versao.id}")


def servir(pasta, porta):
    from werkzeug.serving import run_simple

    if not (pasta / "demo.sqlite").is_file():
        raise SystemExit("Prepare a demonstração antes: --pasta sem --servir.")
    print(f"API SINTÉTICA em http://127.0.0.1:{porta} (Ctrl+C encerra)")
    run_simple("127.0.0.1", porta, criar_app(pasta), threaded=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pasta", type=Path, required=True, help="pasta vazia, fora do repositório")
    parser.add_argument("--servir", action="store_true", help="serve a API da demonstração")
    parser.add_argument("--porta", type=int, default=5000)
    args = parser.parse_args()
    servir(args.pasta.resolve(), args.porta) if args.servir else preparar(args.pasta.resolve())
