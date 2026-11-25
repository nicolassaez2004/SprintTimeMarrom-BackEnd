import csv
import io
import json
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app import create_app, db
from app.exportacoes.csv import (
    CAMPOS,
    FONTE,
    FONTE_URL,
    LICENCA,
    LICENCA_URL,
    publicar_csv_indicadores,
)
from app.exportacoes.parquet import periodo_utc
from app.models import (ArquivoArmazenado, ExecucaoProcessamento, IndicadorOperacional,
                        Linha, VersaoGTFS)

UTC = timezone.utc
DIA = date(2026, 11, 24)
VERSAO = "00000000-0000-0000-0000-000000000024"


def registrar_consolidacao(dia, finalizada_em):
    inicio, fim = periodo_utc(dia)
    chave = f"US23-T03:CONSOLIDACAO_DIA:{dia.isoformat()}"
    db.session.add(ExecucaoProcessamento(
        chave_logica=chave, tipo="CONSOLIDACAO_DIA", estado="PRONTO",
        periodo_inicio=inicio, periodo_fim=fim, bruto_id_maximo=0,
        versao_base="viagens_observadas", versao_codigo="teste",
        versao_regra="teste", tentativas=1, criada_em=finalizada_em,
        iniciada_em=finalizada_em, finalizada_em=finalizada_em, parametros={},
    ))
    db.session.commit()


@pytest.fixture
def cliente(tmp_path):
    app = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite://",
        "MYSQL_SSL_CA": None,
        "EXPORTACOES_DIR": str(tmp_path),
    })
    with app.app_context():
        db.create_all()
        arquivo = ArquivoArmazenado(
            chave="gtfs/teste.zip", nome_original="teste.zip", mime_type="application/zip",
            tamanho_bytes=1, sha256="a" * 64,
        )
        versao = VersaoGTFS(
            id=VERSAO, hash="a" * 64, vigencia_inicio=date(2026, 1, 1), arquivo=arquivo,
        )
        linha = Linha(versao_gtfs=versao, codigo="8000-10", nome="São José — Centro")
        db.session.add_all([versao, linha])
        db.session.flush()
        # Dia civil de São Paulo começa às 03:00 UTC; a linha seguinte é do dia anterior.
        db.session.add_all([
            IndicadorOperacional(
                versao_gtfs_id=VERSAO, linha_codigo=linha.codigo, sentido="0",
                indicador="PONTUALIDADE_VIAGENS", valor=87.5, unidade="%",
                periodo_inicio=datetime(2026, 11, 24, 3, 15, tzinfo=UTC),
                periodo_fim=datetime(2026, 11, 24, 3, 30, tzinfo=UTC),
                contagem_numerador=7, contagem_denominador=8,
                versao_conjunto="conjunto-1", versao_correspondencia="match-1",
                versao_calculo="pontualidade-1", qualidade={"observação": "válida"},
            ),
            IndicadorOperacional(
                versao_gtfs_id=VERSAO, linha_codigo=linha.codigo, sentido="1",
                indicador="PONTUALIDADE_VIAGENS", valor=None, unidade="%",
                periodo_inicio=datetime(2026, 11, 24, 3, 30, tzinfo=UTC),
                periodo_fim=datetime(2026, 11, 24, 3, 45, tzinfo=UTC),
                contagem_numerador=0, contagem_denominador=0,
                versao_conjunto="conjunto-1", qualidade={"dados_insuficientes": True},
            ),
            IndicadorOperacional(
                versao_gtfs_id=VERSAO, linha_codigo=linha.codigo, sentido="0",
                indicador="PONTUALIDADE_VIAGENS", valor=50, unidade="%",
                periodo_inicio=datetime(2026, 11, 24, 2, 45, tzinfo=UTC),
                periodo_fim=datetime(2026, 11, 24, 3, 0, tzinfo=UTC),
                contagem_numerador=1, contagem_denominador=2,
                versao_conjunto="conjunto-1",
            ),
        ])
        db.session.commit()
        registrar_consolidacao(DIA, datetime(2026, 11, 25, 7, tzinfo=UTC))
        publicar_csv_indicadores(DIA, datetime(2026, 11, 25, 7, tzinfo=UTC))
        yield app.test_client()
        db.session.remove()
        db.drop_all()


def test_csv_consolida_dia_confere_com_api_preserva_acentos_e_unidade(cliente):
    resposta = cliente.get(f"/downloads/indicadores?formato=csv&data={DIA.isoformat()}")
    assert resposta.status_code == 200
    assert resposta.mimetype == "text/csv"
    assert resposta.headers["Content-Disposition"].endswith('indicadores_2026-11-24.csv"')
    assert resposta.headers["X-Licenca"] == LICENCA
    assert resposta.headers["X-Fonte"] == FONTE
    assert resposta.headers["X-Fonte-URL"] == FONTE_URL
    assert resposta.headers["X-Licenca-URL"] == LICENCA_URL
    instante_publicacao = datetime.fromisoformat(resposta.headers["X-Disponibilizado-Em"])
    assert instante_publicacao.tzinfo is not None
    assert instante_publicacao.astimezone(ZoneInfo("America/Sao_Paulo")).hour == 4
    assert resposta.headers["Link"].startswith("</downloads/indicadores/metadados>")
    assert resposta.data.startswith(b"\xef\xbb\xbf")

    recibo_path = Path(cliente.application.config["EXPORTACOES_DIR"]) / "indicadores" / (
        f"indicadores_{DIA.isoformat()}.csv.publicacao.json"
    )
    recibo = json.loads(recibo_path.read_text(encoding="utf-8"))
    assert recibo["gerado_em"] == instante_publicacao.isoformat()
    assert recibo["fonte"] == FONTE
    assert recibo["licenca"] == LICENCA
    assert recibo["registros"] == 2
    assert recibo["tamanho_bytes"] == len(resposta.data)
    assert len(recibo["sha256"]) == 64

    leitor_csv = csv.DictReader(io.StringIO(resposta.data.decode("utf-8-sig")))
    assert leitor_csv.fieldnames == list(CAMPOS)
    linhas_csv = list(leitor_csv)
    amostra = Path(__file__).parents[2] / "docs/evidencias/US10-T03-indicadores-sintetico.csv"
    amostra_csv = list(csv.DictReader(io.StringIO(amostra.read_text(encoding="utf-8-sig"))))
    assert linhas_csv == amostra_csv
    inicio = "2026-11-24T03:00:00Z"
    fim = "2026-11-25T03:00:00Z"
    api = cliente.get(
        f"/indicadores?inicio={inicio}&fim={fim}&page_size=100"
    ).get_json()["dados"]

    assert len(linhas_csv) == len(api) == 2
    for linha_csv, indicador_api in zip(linhas_csv, api):
        assert linha_csv["linha"] == indicador_api["linha"]
        assert linha_csv["nome_linha"] == "São José — Centro"
        assert linha_csv["sentido"] == indicador_api["sentido"]
        assert linha_csv["tipo"] == indicador_api["tipo"]
        assert (float(linha_csv["valor"]) if linha_csv["valor"] else None) == indicador_api["valor"]
        assert linha_csv["unidade"] == indicador_api["unidade"] == "%"
        assert linha_csv["inicio"] == indicador_api["inicio"]
        assert linha_csv["fim"] == indicador_api["fim"]
        assert linha_csv["parada"] == (indicador_api["parada"] or "")
        assert linha_csv["trecho"] == (indicador_api["trecho"] or "")
        assert linha_csv["versao_gtfs_id"] == indicador_api["versao_gtfs_id"]
        assert linha_csv["versao_conjunto"] == indicador_api["versao_conjunto"]
        assert linha_csv["versao_correspondencia"] == (indicador_api["versao_correspondencia"] or "")
        assert linha_csv["versao_calculo"] == (indicador_api["versao_calculo"] or "")
        assert int(linha_csv["contagem_numerador"]) == indicador_api["contagens"]["numerador"]
        assert int(linha_csv["contagem_denominador"]) == indicador_api["contagens"]["denominador"]
        assert json.loads(linha_csv["qualidade"]) == indicador_api["qualidade"]
    assert linhas_csv[1]["valor"] == ""


def test_periodo_nao_consolidado_nao_entrega_csv_vazio(cliente):
    resposta = cliente.get("/downloads/indicadores?formato=csv&data=2026-11-25")
    assert resposta.status_code == 404
    assert resposta.mimetype == "application/json"
    assert resposta.get_json()["erro"]["codigo"] == "PERIODO_AINDA_NAO_CONSOLIDADO"
    assert "previsto_em" in resposta.get_json()["erro"]
    evidencia = Path(__file__).parents[2] / "docs/evidencias/US10-T03-periodo-indisponivel.json"
    registro = json.loads(evidencia.read_text(encoding="utf-8"))
    assert resposta.status_code == registro["status_http"]
    assert resposta.get_json() == registro["data"]


def test_dia_consolidado_sem_indicadores_entrega_csv_com_cabecalho(cliente):
    dia_vazio = date(2026, 11, 22)
    consolidado_em = datetime(2026, 11, 23, 7, tzinfo=UTC)
    with cliente.application.app_context():
        registrar_consolidacao(dia_vazio, consolidado_em)
        publicar_csv_indicadores(dia_vazio, consolidado_em)

    resposta = cliente.get(f"/downloads/indicadores?formato=csv&data={dia_vazio.isoformat()}")
    assert resposta.status_code == 200
    assert resposta.headers["X-Total-Registros"] == "0"
    leitor = csv.DictReader(io.StringIO(resposta.data.decode("utf-8-sig")))
    assert leitor.fieldnames[0] == "linha"
    assert list(leitor) == []


def test_metadados_e_filtro_de_linha_sentido_periodo(cliente):
    resposta = cliente.get("/downloads/indicadores?formato=csv&data=2026-11-24&linha=8000-10&sentido=ida")
    assert resposta.status_code == 200
    leitor = csv.DictReader(io.StringIO(resposta.data.decode("utf-8-sig")))
    registros = list(leitor)
    assert len(registros) == 1
    assert registros[0]["sentido"] == "0"
    assert resposta.headers["X-Total-Registros"] == "1"
    metadados = cliente.get("/downloads/indicadores/metadados").get_json()
    assert metadados["licenca"] == LICENCA
    assert metadados["fonte"] == FONTE
    assert metadados["fonte_url"] == FONTE_URL
    assert metadados["licenca_url"] == LICENCA_URL
    # US10: mesma licença da US09 (CC BY 4.0); as condições da SPTrans seguem informadas.
    assert LICENCA == "CC BY 4.0" and "creativecommons.org/licenses/by/4.0" in LICENCA_URL
    assert "não podem ser alugados" in metadados["condicoes_fonte"]
    assert "sublicenciados ou vendidos" in metadados["condicoes_fonte"]
    assert registros[0]["fonte"] == FONTE and registros[0]["licenca"] == LICENCA
    assert metadados["nulos"] == "campos vazios"


def test_filtro_de_periodo_pode_abranger_o_dia_local_completo(cliente):
    resposta = cliente.get(
        "/downloads/indicadores?formato=csv&inicio=2026-11-24T03:00:00Z"
        "&fim=2026-11-25T03:00:00Z"
    )
    assert resposta.status_code == 200
    leitor = csv.DictReader(io.StringIO(resposta.data.decode("utf-8-sig")))
    assert len(list(leitor)) == 2


@pytest.mark.parametrize("query", [
    "&data=2026-02-30", "&data=2026-11-24&inicio=2026-11-24T00:00:00Z",
    "&data=2026-11-24&data=2026-11-25", "&desconhecido=1",
])
def test_csv_rejeita_periodos_invalidos_e_parametros_desconhecidos(cliente, query):
    resposta = cliente.get("/downloads/indicadores?formato=csv" + query)
    assert resposta.status_code == 400
    assert resposta.get_json()["erro"]["codigo"] == "PARAMETRO_INVALIDO"


def test_filtro_por_tipo_publico_da_api_funciona_no_csv(cliente):
    """Regressão: tipo=REGULARIDADE_INTERVALOS/ESPERA/FREQUENCIA exigia as regras de
    agregação de /indicadores (inicio/fim) e respondia 400 com data=."""
    for tipo in ("REGULARIDADE_INTERVALOS", "ESPERA", "FREQUENCIA"):
        resposta = cliente.get(f"/downloads/indicadores?formato=csv&data=2026-11-24&tipo={tipo}")
        assert resposta.status_code == 200, resposta.get_json()
        assert resposta.headers["X-Total-Registros"] == "0"
    resposta = cliente.get("/downloads/indicadores?formato=csv&data=2026-11-24&indicador=PONTUALIDADE_VIAGENS")
    assert resposta.headers["X-Total-Registros"] == "2"
    duplo = cliente.get("/downloads/indicadores?formato=csv&data=2026-11-24&tipo=A&indicador=B")
    assert duplo.status_code == 400
