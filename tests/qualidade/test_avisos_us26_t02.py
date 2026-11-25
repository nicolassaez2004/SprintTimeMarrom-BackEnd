"""US26-T02: qualidade de coleta acompanha indicadores, mapas e CSVs publicados."""

import csv
import io
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import create_app, db
from app.exportacoes.csv import caminho_indicadores, publicar_csv_indicadores
from app.exportacoes.parquet import periodo_utc
from app.models import (
    ArquivoArmazenado,
    ExecucaoColeta,
    ExecucaoProcessamento,
    IndicadorOperacional,
    LacunaColeta,
    Linha,
    Parada,
    VersaoGTFS,
)

UTC = timezone.utc
DIA = date(2026, 11, 25)
INICIO = datetime(2026, 11, 25, 3, tzinfo=UTC)
FIM = INICIO + timedelta(minutes=15)
VERSAO = "00000000-0000-0000-0000-000000000026"


@pytest.fixture
def cliente(tmp_path):
    app = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite://",
        "MYSQL_SSL_CA": None,
        "GPS_COLLECTION_INTERVAL_SECONDS": 30,
        "EXPORTACOES_DIR": str(tmp_path),
    })
    with app.app_context():
        db.create_all()
        arquivo = ArquivoArmazenado(
            chave="gtfs/us26.zip", nome_original="us26.zip", mime_type="application/zip",
            tamanho_bytes=1, sha256="a" * 64,
        )
        versao = VersaoGTFS(
            id=VERSAO, hash="a" * 64, vigencia_inicio=date(2026, 1, 1), arquivo=arquivo,
        )
        linha = Linha(versao_gtfs=versao, codigo="8000-10", nome="Linha da amostra")
        parada = Parada(versao_gtfs_id=VERSAO, codigo="P1", nome="Parada da amostra",
                        longitude=-46.63, latitude=-23.55)
        lacuna = LacunaColeta(
            id="lacuna-us26", inicio=INICIO + timedelta(minutes=13), fim=FIM,
            causa="SPTRANS_HTTP_503", fonte="OLHO_VIVO",
        )
        db.session.add_all([versao, linha, parada, lacuna])
        db.session.flush()

        for indice in range(30):
            instante = INICIO + timedelta(seconds=30 * indice)
            falha = indice >= 26
            db.session.add(ExecucaoColeta(
                prevista_em=instante, iniciada_em=instante,
                finalizada_em=instante + timedelta(seconds=1),
                estado="FALHA" if falha else "OK",
                erro="SPTRANS_HTTP_503" if falha else None,
                lacuna_id=lacuna.id if falha else None,
            ))

        db.session.add(IndicadorOperacional(
            versao_gtfs_id=VERSAO, linha_codigo=linha.codigo, sentido="0",
            parada_codigo=parada.codigo, indicador="PONTUALIDADE_VIAGENS",
            valor=80.0, unidade="%", periodo_inicio=INICIO, periodo_fim=FIM,
            contagem_numerador=4, contagem_denominador=5,
            versao_conjunto="conjunto-us26", versao_correspondencia="match-us26",
            versao_calculo="calculo-us26", qualidade={"dados_insuficientes": False},
        ))
        inicio_dia, fim_dia = periodo_utc(DIA)
        publicada_em = FIM
        db.session.add(ExecucaoProcessamento(
            chave_logica=f"US23-T03:CONSOLIDACAO_DIA:{DIA.isoformat()}",
            tipo="CONSOLIDACAO_DIA", estado="PRONTO", periodo_inicio=inicio_dia,
            periodo_fim=fim_dia, bruto_id_maximo=30, versao_base="viagens_observadas",
            versao_codigo="us26-test", versao_regra="us26-test", tentativas=1,
            criada_em=publicada_em, iniciada_em=publicada_em, finalizada_em=publicada_em,
            parametros={},
        ))
        db.session.commit()
        publicar_csv_indicadores(DIA, FIM)
        yield app.test_client()
        db.session.remove()
        db.drop_all()


def test_aviso_acima_de_dez_porcento_permanece_alinhado_na_api_mapa_e_csv(cliente):
    inicio, fim = INICIO.isoformat(), FIM.isoformat()
    resposta_api = cliente.get("/indicadores", query_string={
        "linha": "8000-10", "inicio": inicio, "fim": fim,
    })
    assert resposta_api.status_code == 200
    indicador = resposta_api.json["dados"][0]
    aviso_api = indicador["qualidade_coleta"]
    assert aviso_api["percentual_falhas"] == pytest.approx(13.33)
    assert aviso_api["incompleto"] is True
    assert aviso_api["motivo"] == "SPTRANS_HTTP_503"
    assert aviso_api["versao_gtfs_id"] == indicador["versao_gtfs_id"] == VERSAO
    assert aviso_api["inicio"] == indicador["inicio"] == inicio
    assert aviso_api["fim"] == indicador["fim"] == fim

    evidencia_api = json.loads(
        (Path(__file__).parents[2] / "docs/evidencias/US26-T02-api.json").read_text(encoding="utf-8")
    )
    assert evidencia_api["status_http"] == resposta_api.status_code
    assert evidencia_api["indicador"]["qualidade_coleta"] == aviso_api

    resposta_mapa = cliente.get("/mapas/indicadores", query_string={
        "escopo": "parada", "versao_gtfs_id": VERSAO, "inicio": inicio, "fim": fim,
    })
    assert resposta_mapa.status_code == 200
    feature = resposta_mapa.json["features"][0]
    aviso_mapa = feature["properties"]["qualidade_coleta"]
    assert aviso_mapa == aviso_api

    caminho = caminho_indicadores(DIA)
    with caminho.open(encoding="utf-8-sig", newline="") as arquivo:
        csv_registro = next(csv.DictReader(arquivo))
    aviso_csv = json.loads(csv_registro["qualidade_coleta"])
    assert aviso_csv == aviso_api
    assert csv_registro["inicio"] == inicio
    assert csv_registro["fim"] == fim
    assert csv_registro["versao_gtfs_id"] == VERSAO
    evidencia_csv = Path(__file__).parents[2] / "docs/evidencias/US26-T02-indicadores.csv"
    with evidencia_csv.open(encoding="utf-8-sig", newline="") as arquivo:
        registro_evidencia = next(csv.DictReader(arquivo))
    assert json.loads(registro_evidencia["qualidade_coleta"]) == aviso_csv
