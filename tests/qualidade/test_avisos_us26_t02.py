"""US26-T02: a qualidade publicada (US26-T01) acompanha API, mapas e CSV (dados SINTÉTICOS).

O indicador é gravado pelo caminho real (``gravar_indicador`` + ``FalhasColeta``). O resumo
público ``qualidade_coleta`` deriva só do bloco gravado: não é recalculado na consulta,
logo API, mapa e CSV da mesma publicação nunca divergem. SQLite em memória.
"""

import csv
import io
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from app import create_app, db
from app.exportacoes.csv import caminho_indicadores, publicar_csv_indicadores
from app.exportacoes.parquet import periodo_utc
from app.indicadores.cumprimento import gravar_indicador
from app.models import (
    ArquivoArmazenado, ExecucaoColeta, ExecucaoProcessamento, IndicadorOperacional, LacunaColeta,
    Linha, Parada, ParadaTerritorial, UnidadeTerritorial, VersaoGTFS, VersaoTerritorial,
)
from app.qualidade.anotacoes import resumo_coleta
from app.qualidade.service import FalhasColeta

UTC = timezone.utc
DIA = date(2026, 11, 25)
W1 = datetime(2026, 11, 25, 3, tzinfo=UTC)          # 00:00 em São Paulo
W2, W3 = W1 + timedelta(minutes=15), W1 + timedelta(minutes=30)
VERSAO = "00000000-0000-0000-0000-000000000026"
LINHA = "8000-10"
QUADRADO = {"type": "Polygon", "coordinates": [[[-47, -24], [-46, -24], [-46, -23], [-47, -23], [-47, -24]]]}


def ciclos(inicio, falhas=(), ausentes=()):
    """30 ciclos de 30 s; ``falhas`` com FALHA (lacuna HTTP 503), ``ausentes`` sem registro."""
    for k in range(30):
        if k in ausentes:
            continue
        instante = inicio + timedelta(seconds=30 * k)
        db.session.add(ExecucaoColeta(prevista_em=instante, iniciada_em=instante,
                                      finalizada_em=instante + timedelta(seconds=1),
                                      estado="FALHA" if k in falhas else "OK",
                                      erro="SPTRANS_HTTP_503" if k in falhas else None))


def publicar(parada, inicio, valor=80.0, agora=None):
    return gravar_indicador({
        "chave_resultado": f"us26-{parada}-{inicio.isoformat()}", "versao_gtfs_id": VERSAO,
        "linha_codigo": LINHA, "sentido": "0", "parada_codigo": parada,
        "indicador": "PONTUALIDADE_VIAGENS", "valor": valor, "unidade": "%",
        "periodo_inicio": inicio, "periodo_fim": inicio + timedelta(minutes=15),
        "contagem_numerador": 4, "contagem_denominador": 5, "versao_conjunto": "conjunto-us26",
        "versao_correspondencia": "match-us26", "versao_calculo": "calculo-us26",
        "qualidade": {"dados_insuficientes": False},
    }, falhas=FalhasColeta(agora=agora or W3 + timedelta(minutes=16)))


@pytest.fixture
def cliente(tmp_path):
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://", "MYSQL_SSL_CA": None,
                      "GPS_COLLECTION_INTERVAL_SECONDS": 30, "EXPORTACOES_DIR": str(tmp_path)})
    with app.app_context():
        db.create_all()
        arquivo = ArquivoArmazenado(chave="gtfs/us26.zip", nome_original="us26.zip",
                                    mime_type="application/zip", tamanho_bytes=1, sha256="a" * 64)
        versao = VersaoGTFS(id=VERSAO, hash="a" * 64, vigencia_inicio=date(2026, 1, 1), arquivo=arquivo)
        territorio = VersaoTerritorial(fonte="SINTETICA", url_fonte="-", versao_fonte="us26",
                                       sha256="b" * 64, crs_origem="EPSG:4326")
        db.session.add_all([versao, Linha(versao_gtfs=versao, codigo=LINHA, nome="Linha da amostra"),
                            territorio])
        db.session.flush()
        sub = UnidadeTerritorial(versao_id=territorio.id, tipo="SUBPREFEITURA", codigo="S1", nome="Sub",
                                 identificador_fonte="S1", geometria=QUADRADO)
        db.session.add(sub)
        db.session.flush()
        distrito = UnidadeTerritorial(versao_id=territorio.id, tipo="DISTRITO", codigo="D1", nome="Distrito",
                                      identificador_fonte="D1", subprefeitura_id=sub.id, geometria=QUADRADO)
        db.session.add(distrito)
        db.session.flush()
        for codigo in ("P1", "P2"):
            db.session.add(Parada(versao_gtfs_id=VERSAO, codigo=codigo, nome=f"Parada {codigo}",
                                  longitude=-46.63, latitude=-23.55))
            db.session.add(ParadaTerritorial(versao_gtfs_id=VERSAO, parada_codigo=codigo,
                                             versao_territorial_id=territorio.id,
                                             distrito_id=distrito.id, subprefeitura_id=sub.id))
        db.session.add(LacunaColeta(id="lacuna-us26", inicio=W1 + timedelta(minutes=13), fim=W2,
                                    causa="SPTRANS_HTTP_503", fonte="OLHO_VIVO"))
        ciclos(W1, falhas=range(26, 30))       # 4/30 = 13,33%  → aviso
        ciclos(W2, ausentes=(10, 11, 12))      # 3/30 = 10,00%  → sem aviso (limite estrito)
        ciclos(W3)                             # 0/30 = 0%      → sem aviso
        db.session.commit()
        for inicio in (W1, W2, W3):
            for parada in ("P1", "P2"):
                publicar(parada, inicio)
        inicio_dia, _ = periodo_utc(DIA)
        db.session.add(ExecucaoProcessamento(
            chave_logica=f"US23-T03:CONSOLIDACAO_DIA:{DIA.isoformat()}", tipo="CONSOLIDACAO_DIA",
            estado="PRONTO", periodo_inicio=inicio_dia, periodo_fim=inicio_dia + timedelta(days=1),
            bruto_id_maximo=0, versao_base="viagens_observadas", versao_codigo="t", versao_regra="t",
            tentativas=1, criada_em=W3, iniciada_em=W3, finalizada_em=W3, parametros={}))
        db.session.commit()
        publicar_csv_indicadores(DIA, W3 + timedelta(hours=2))
        yield app.test_client()
        db.session.remove()
        db.drop_all()


def resumo(q):
    return q["percentual_falhas"], q["incompleto"], q["situacao"], q["motivo"]


def api(cliente, inicio, **extra):
    resposta = cliente.get("/indicadores", query_string={
        "linha": LINHA, "parada": "P1", "inicio": inicio.isoformat(),
        "fim": (inicio + timedelta(minutes=15)).isoformat(), **extra})
    assert resposta.status_code == 200, resposta.get_json()
    return resposta.get_json()["dados"]


def mapa(cliente, inicio, **extra):
    resposta = cliente.get("/mapas/indicadores", query_string={
        "versao_gtfs_id": VERSAO, "tipo": "PONTUALIDADE_VIAGENS", "inicio": inicio.isoformat(),
        "fim": (inicio + timedelta(minutes=15)).isoformat(), **extra})
    assert resposta.status_code == 200, resposta.get_json()
    return resposta.get_json()["features"]


def csv_diario():
    with caminho_indicadores(DIA).open(encoding="utf-8-sig", newline="") as arquivo:
        return list(csv.DictReader(arquivo))


@pytest.mark.parametrize("inicio, esperado", [
    (W1, (13.33, True, "INCOMPLETA", "FALHAS_ACIMA_DO_LIMITE")),
    (W2, (10.0, False, "COM_FALHAS", None)),
    (W3, (0.0, False, "COMPLETA", None)),
])
def test_api_mapa_e_csv_publicam_o_mesmo_aviso(cliente, inicio, esperado):
    [item] = api(cliente, inicio)
    q = item["qualidade_coleta"]
    assert resumo(q) == esperado
    assert (q["inicio"], q["fim"], q["versao_gtfs_id"]) == (item["inicio"], item["fim"], VERSAO)
    assert (q["escopo"], q["limite_incompleto_percentual"]) == ("COLETA_GLOBAL", 10.0)
    # Deriva do bloco gravado pela US26-T01, sem outra regra.
    bloco = item["qualidade"]["falhas_coleta"]
    assert (q["percentual_falhas"], q["incompleto"]) == (bloco["percentual_falhas"], bloco["incompleto"])
    assert (q["contagens"]["falhas"], q["contagens"]["denominador"]) == (bloco["falhas"], bloco["denominador"])

    feicao = next(f for f in mapa(cliente, inicio, escopo="parada") if f["properties"]["parada"] == "P1")
    assert feicao["properties"]["qualidade_coleta"] == q
    linha_csv = next(r for r in csv_diario() if r["parada"] == "P1" and r["inicio"] == item["inicio"])
    assert json.loads(linha_csv["qualidade_coleta"]) == q


def test_causa_da_lacuna_acompanha_o_aviso(cliente):
    [item] = api(cliente, W1)
    assert item["qualidade_coleta"]["causas"] == ["SPTRANS_HTTP_503"]
    assert item["qualidade_coleta"]["contagens"]["ciclos_falha"] == 4


def test_territorio_soma_janelas_sem_contar_a_coleta_global_duas_vezes(cliente):
    for escopo in ("distrito", "subprefeitura"):
        [feicao] = mapa(cliente, W1, escopo=escopo, base="parada")
        q = feicao["properties"]["qualidade_coleta"]
        assert feicao["properties"]["registros_agregados"] == 2      # P1 e P2
        assert (q["contagens"]["falhas"], q["contagens"]["denominador"]) == (4, 30)  # não 8/60
        assert resumo(q) == (13.33, True, "INCOMPLETA", "FALHAS_ACIMA_DO_LIMITE")


def test_consulta_nao_recalcula_e_republicacao_atualiza(cliente):
    antes = api(cliente, W1)[0]["qualidade_coleta"]
    # Dado atrasado: os 4 ciclos com falha passam a OK. Sem republicar, nada muda (= CSV publicado).
    for e in ExecucaoColeta.query.filter_by(estado="FALHA"):
        e.estado, e.erro = "OK", None
    db.session.commit()
    assert api(cliente, W1)[0]["qualidade_coleta"] == antes
    linha_csv = next(r for r in csv_diario() if r["parada"] == "P1" and r["inicio"] == W1.isoformat())
    assert json.loads(linha_csv["qualidade_coleta"]) == antes
    # O recálculo (US20/US23-T03) republica e a resposta seguinte reflete a nova qualidade.
    publicar("P1", W1, agora=W3 + timedelta(hours=3))
    depois = api(cliente, W1)[0]["qualidade_coleta"]
    assert depois["incompleto"] is False and depois["percentual_falhas"] < 10


def test_indicador_publicado_sem_bloco_fica_indeterminado(cliente):
    registro = IndicadorOperacional.query.filter_by(parada_codigo="P1", periodo_inicio=W3).one()
    registro.qualidade = {"dados_insuficientes": False}  # publicado antes da US26-T01
    db.session.commit()
    q = api(cliente, W3)[0]["qualidade_coleta"]
    assert resumo(q) == (None, None, "NAO_REGISTRADA", "PUBLICADO_SEM_QUALIDADE_DE_COLETA")


def _bloco(inicio, falhas, denominador, com_dados, situacao="COM_FALHAS", motivo=None):
    return {"inicio": inicio.isoformat(), "fim": (inicio + timedelta(minutes=15)).isoformat(),
            "falhas": falhas, "denominador": denominador, "ciclos_com_dados": com_dados,
            "situacao": situacao, "motivo": motivo, "lacunas": []}


ITEM = {"inicio": W1.isoformat(), "fim": (W1 + timedelta(hours=1)).isoformat(), "versao_gtfs_id": VERSAO}


@pytest.mark.parametrize("blocos, esperado", [
    # Soma das contagens, não média: (4+0)/(30+10) = 10% exatos → sem aviso (média seria 6,67%).
    ([_bloco(W1, 4, 30, 26), _bloco(W2, 0, 10, 10)], (10.0, False, "COM_FALHAS", None)),
    ([_bloco(W1, 101, 1000, 899)], (10.1, True, "INCOMPLETA", "FALHAS_ACIMA_DO_LIMITE")),
    ([_bloco(W1, 30, 30, 0)], (100.0, True, "SEM_DADOS", "NENHUM_CICLO_COM_DADOS")),
    # Denominador zero: nulo e motivo explícito, nunca "coleta completa".
    ([_bloco(W1, 0, 0, 0, "SEM_EXPECTATIVA", "FORA_DO_MONITORAMENTO")],
     (None, None, "SEM_EXPECTATIVA", "FORA_DO_MONITORAMENTO")),
    ([_bloco(W1, 0, 30, 30), None], (None, None, "NAO_REGISTRADA", "PUBLICADO_SEM_QUALIDADE_DE_COLETA")),
])
def test_agregacao_dos_blocos(blocos, esperado):
    assert resumo(resumo_coleta(blocos, ITEM)) == esperado


def test_item_sem_periodo():
    assert resumo_coleta([], {"inicio": None})["motivo"] == "SEM_PERIODO_ASSOCIADO"
