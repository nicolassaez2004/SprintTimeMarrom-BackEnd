"""Review US07 (T01/T02/T03): casos extremos de importação, classificação e histórico.

Dados sintéticos em SQLite; não comprovam importação nem operação mensal reais.
"""
from datetime import date, datetime, timezone
import gzip
import hashlib
import json

import pytest
from pyproj import Geod, Transformer

from app import create_app, db
from app.contexto.infraestrutura.agenda import atualizar_mensal, registrar_classificacao_vigente
from app.contexto.infraestrutura.associacao import (
    VERSAO_REGRA, classificar_trechos, conjunto_de, detalhar_trecho,
)
from app.contexto.infraestrutura.importacao import importar_geojson
from app.models import (
    AssociacaoInfraestruturaTrecho, AtualizacaoMensalInfraestrutura, ClassificacaoInfraestrutura,
    Trecho, VersaoGTFS, VersaoInfraestrutura,
)

UTC = timezone.utc
URL = "https://example.test/fonte"
LAT = -23.55
GEOD = Geod(ellps="WGS84")


@pytest.fixture
def app(tmp_path):
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://", "MYSQL_SSL_CA": None})
    app.instance_path = str(tmp_path / "instance")
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


def fc(*features, crs=None):
    colecao = {"type": "FeatureCollection", "features": list(features)}
    return {**colecao, "crs": {"type": "name", "properties": {"name": crs}}} if crs else colecao


def feat(fid, *coords, props=None):
    return {"type": "Feature", "id": fid, "properties": props or {},
            "geometry": {"type": "LineString", "coordinates": [list(c) for c in coords]}}


def importar(dados, categoria="CORREDOR", fonte="GeoSampa", quando=None, **extra):
    return importar_geojson(dados, fonte=fonte, url_fonte=URL, versao_fonte=str(quando or "v"),
                            crs_origem=extra.pop("crs", "EPSG:4326"), categoria=categoria,
                            capturada_em=quando, **extra)


def trecho(tid, *coords, rede="gtfs-1", ordem=1):
    db.session.add(Trecho(id=tid, versao_gtfs_id=rede, linha_codigo="L1", sentido="0", trajeto="T1",
                          ordem=ordem, parada_origem_codigo=f"P{ordem}",
                          parada_destino_codigo=f"P{ordem + 1}", geometria=[list(c) for c in coords]))
    db.session.commit()


def classe(tid):
    return AssociacaoInfraestruturaTrecho.query.filter_by(trecho_id=tid).one()


# --- T01: importação ----------------------------------------------------------

def test_utm_31983_e_convertido_para_lon_lat_sem_perda(app):
    """Coordenadas de referência (Av. Paulista) ida e volta pelo SIRGAS 2000/UTM 23S."""
    paulista = [(-46.6566, -23.5614), (-46.6520, -23.5650)]
    para_utm = Transformer.from_crs("EPSG:4326", "EPSG:31983", always_xy=True)
    utm = [para_utm.transform(*p) for p in paulista]
    assert 300_000 < utm[0][0] < 400_000 and 7_300_000 < utm[0][1] < 7_500_000
    versao = importar(fc(feat("p", *utm), crs="urn:ogc:def:crs:EPSG::31983"), crs="EPSG:31983")
    coords = ClassificacaoInfraestrutura.query.one().geometria["coordinates"]
    for (lon, lat), (lon_ref, lat_ref) in zip(coords, paulista):
        assert (lon, lat) == pytest.approx((lon_ref, lat_ref), abs=1e-7)  # ordem x=lon, y=lat
    assert versao.crs_origem == "EPSG:31983" and versao.metadados["crs_armazenado"] == "EPSG:4326"


@pytest.mark.parametrize("coords, crs, trecho_erro", [
    (((330000, 7395000), (330100, 7395000)), "EPSG:4326", "fora da região"),   # UTM declarado como graus
    (((-23.5, -46.6), (-23.4, -46.5)), "EPSG:4326", "fora da região"),          # latitude/longitude trocadas
])
def test_coordenadas_em_crs_errado_ou_invertidas_sao_rejeitadas(app, coords, crs, trecho_erro):
    with pytest.raises(ValueError, match=trecho_erro):
        importar(fc(feat("x", *coords)), crs=crs)


def test_crs_declarado_no_arquivo_divergente_e_rejeitado(app):
    with pytest.raises(ValueError, match="diverge"):
        importar(fc(feat("x", (-46.6, LAT), (-46.59, LAT)), crs="urn:ogc:def:crs:EPSG::31983"))


@pytest.mark.parametrize("features, mensagem", [
    ([feat("d", (-46.6, LAT), (-46.59, LAT)), feat("d", (-46.6, -23.6), (-46.59, -23.6))], "repetido"),
    ([{"type": "Feature", "id": "n", "properties": {}, "geometry": None}], "sem geometria"),
    ([{"type": "Feature", "id": "p", "properties": {},
       "geometry": {"type": "Point", "coordinates": [-46.6, LAT]}}], "linear"),
    ([feat("z", (-46.6, LAT), (-46.6, LAT))], "linear válida"),  # comprimento zero
])
def test_feicoes_invalidas_ou_duplicadas_geram_erro_claro_sem_gravar(app, features, mensagem):
    with pytest.raises(ValueError, match=mensagem):
        importar(fc(*features))
    assert VersaoInfraestrutura.query.count() == 0 and ClassificacaoInfraestrutura.query.count() == 0


def test_arquivo_gz_registra_hash_do_conteudo_e_dados_de_reproducao(app, tmp_path):
    dados = fc(feat("c1", (-46.6, LAT), (-46.59, LAT), props={"cod": "C-1"}))
    bruto = json.dumps(dados).encode()
    arquivo = tmp_path / "corredores-2026-11-01.geojson.gz"
    arquivo.write_bytes(gzip.compress(bruto))
    versao = importar(str(arquivo), id_field="cod")
    assert versao.sha256 == hashlib.sha256(bruto).hexdigest()
    assert versao.metadados["arquivo_origem"] == arquivo.name and versao.metadados["id_field"] == "cod"
    assert importar(str(arquivo), id_field="cod").id == versao.id  # reimportar é idempotente
    with pytest.raises(ValueError, match="já foi importado"):
        importar(str(arquivo), categoria="FAIXA_EXCLUSIVA", id_field="cod")


def test_mudancas_comparam_somente_a_mesma_camada(app):
    """Antes: a camada de faixas era comparada com a de corredores (tudo REMOVIDA/ADICIONADA)."""
    t = datetime(2026, 11, 1, tzinfo=UTC)
    importar(fc(feat("c1", (-46.6, LAT), (-46.59, LAT))), "CORREDOR", quando=t)
    faixas = importar(fc(feat("f1", (-46.5, LAT), (-46.49, LAT))), "FAIXA_EXCLUSIVA",
                      quando=t.replace(minute=1))
    assert faixas.metadados["versao_anterior_id"] is None
    assert [(m["identificador_fonte"], m["tipo"]) for m in faixas.metadados["mudancas"]] == [("f1", "ADICIONADA")]


# --- T02: classificação -------------------------------------------------------

def _rede_basica():
    """Corredor GeoSampa em -46.60..-46.59 e faixa em -46.58..-46.57, na latitude LAT."""
    t = datetime(2026, 11, 1, tzinfo=UTC)
    corredor = importar(fc(feat("c1", (-46.60, LAT), (-46.59, LAT))), "CORREDOR", quando=t)
    faixa = importar(fc(feat("f1", (-46.58, LAT), (-46.57, LAT))), "FAIXA_EXCLUSIVA", quando=t)
    return [corredor, faixa]


def test_casos_de_classificacao_espacial(app):
    versoes = _rede_basica()
    trecho("dentro-corredor", (-46.599, LAT), (-46.591, LAT), ordem=1)
    trecho("dentro-faixa", (-46.579, LAT), (-46.571, LAT), ordem=2)
    paralela_30m = LAT + 30 / 111_320  # rua paralela ~30 m ao norte do corredor
    trecho("paralela", (-46.599, paralela_30m), (-46.591, paralela_30m), ordem=3)
    trecho("transversal", (-46.595, LAT - 0.0015), (-46.595, LAT + 0.0015), ordem=4)
    trecho("sem-cobertura", (-46.40, LAT), (-46.39, LAT), ordem=5)
    trecho("degenerado", (-46.40, LAT), (-46.40, LAT), ordem=6)
    resumo = classificar_trechos("gtfs-1", versoes=versoes)
    assert classe("dentro-corredor").classificacao == "CORREDOR"
    assert classe("dentro-faixa").classificacao == "FAIXA_EXCLUSIVA"
    assert classe("paralela").classificacao == "SEM_COBERTURA"   # fora da tolerância de 10 m
    assert classe("sem-cobertura").classificacao == "SEM_COBERTURA"
    assert classe("sem-cobertura").regra["via_comum_confirmada"] is False
    # Transversal: comportamento atual documentado para o PO (≈ 20 m dentro da faixa de 10 m).
    transversal = classe("transversal")
    assert transversal.classificacao == "COBERTURA_PARCIAL"
    assert transversal.categorias_confirmadas == ["CORREDOR"]
    assert transversal.fracao_corredor == pytest.approx(20 / 333.7, rel=0.05)
    # Degenerado: não bloqueia a rede e não recebe classe inventada.
    assert resumo["nao_classificados"] == [{"trecho_id": "degenerado", "motivo": "geometria_sem_comprimento"}]
    assert AssociacaoInfraestruturaTrecho.query.filter_by(trecho_id="degenerado").count() == 0
    assert resumo["trechos"] == 6 and resumo["percentual_classificado"] == round(100 * 2 / 6, 2)
    assert "VIA_COMUM" not in {a.classificacao for a in AssociacaoInfraestruturaTrecho.query}


def test_fracao_medida_em_metros_confere_com_geodesica(app):
    """Trecho de 0,01° de longitude: metade em corredor. Comprimentos em EPSG:31983 ≈ geodésica."""
    versoes = _rede_basica()
    trecho("meio", (-46.595, LAT), (-46.585, LAT))
    classificar_trechos("gtfs-1", versoes=versoes)
    a = classe("meio")
    total = GEOD.line_length([-46.595, -46.585], [LAT, LAT])
    coberto = GEOD.line_length([-46.595, -46.59], [LAT, LAT]) + 10  # +10 m: tolerância após o fim
    assert a.fracao_corredor == pytest.approx(coberto / total, abs=0.01)
    assert a.classificacao == "COBERTURA_PARCIAL"


def test_duas_versoes_gtfs_do_mesmo_trecho_sao_classificadas_separadamente(app):
    versoes = _rede_basica()
    trecho("v1-ab", (-46.599, LAT), (-46.591, LAT), rede="gtfs-1")
    trecho("v2-ab", (-46.40, LAT), (-46.39, LAT), rede="gtfs-2")   # traçado mudou na nova versão
    classificar_trechos("gtfs-1", versoes=versoes)
    classificar_trechos("gtfs-2", versoes=versoes)
    assert (classe("v1-ab").classificacao, classe("v2-ab").classificacao) == ("CORREDOR", "SEM_COBERTURA")


def test_conjunto_inclui_a_regra_e_reclassificar_nao_duplica(app):
    versoes = _rede_basica()
    trecho("ab", (-46.599, LAT), (-46.591, LAT))
    primeiro = classificar_trechos("gtfs-1", versoes=versoes)
    segundo = classificar_trechos("gtfs-1", versoes=versoes)
    assert (primeiro["associacoes_novas"], segundo["associacoes_novas"]) == (1, 0)
    assert AssociacaoInfraestruturaTrecho.query.count() == 1
    ids = sorted(v.id for v in versoes)
    assert primeiro["versao_conjunto"] == conjunto_de(ids) and primeiro["versao_regra"] == VERSAO_REGRA
    assert conjunto_de(ids) != hashlib.sha256(json.dumps(ids, separators=(",", ":")).encode()).hexdigest()


# --- T03: teste obrigatório A → B, histórico, diferenças e idempotência -------

def test_atualizacao_mensal_versao_a_e_b_com_historico_e_sem_duplicar(app):
    db.session.add(VersaoGTFS(id="gtfs-1", hash="h", vigencia_inicio=date(2026, 1, 1), arquivo_id="arq"))
    db.session.commit()
    trecho("ab", (-46.599, LAT), (-46.591, LAT))
    corredor = feat("c1", (-46.60, LAT), (-46.59, LAT))
    versao_a = [{"origem": fc(corredor), "fonte": "GeoSampa", "url_fonte": URL, "versao_fonte": "A",
                 "crs_origem": "EPSG:4326", "categoria": "CORREDOR",
                 "capturada_em": datetime(2026, 10, 1, 3, tzinfo=UTC)}]
    # B: o corredor deixa o trecho (geometria) e o trecho passa a ter faixa exclusiva (categoria).
    versao_b = [
        {**versao_a[0], "origem": fc(feat("c1", (-46.50, LAT), (-46.49, LAT))), "versao_fonte": "B",
         "capturada_em": datetime(2026, 11, 1, 3, tzinfo=UTC)},
        {**versao_a[0], "origem": fc(feat("f9", (-46.60, LAT), (-46.59, LAT))), "versao_fonte": "B",
         "categoria": "FAIXA_EXCLUSIVA", "capturada_em": datetime(2026, 11, 1, 3, tzinfo=UTC)},
    ]

    def rodar(agora, bases):
        atualizar_mensal(agora, lambda _: bases)
        return registrar_classificacao_vigente(agora)

    outubro, novembro = datetime(2026, 10, 1, 4, tzinfo=UTC), datetime(2026, 11, 1, 4, tzinfo=UTC)
    assert rodar(outubro, versao_a)["classificacoes"]["CORREDOR"] == 1
    resumo_b = rodar(novembro, versao_b)
    assert resumo_b["classificacoes"]["FAIXA_EXCLUSIVA"] == 1

    antigo, novo = detalhar_trecho("ab", quando=date(2026, 10, 15)), detalhar_trecho("ab", quando=date(2026, 11, 15))
    assert antigo["classificacao"] == "CORREDOR" and {f["versao_fonte"] for f in antigo["fontes"]} == {"A"}
    assert novo["classificacao"] == "FAIXA_EXCLUSIVA" and {f["versao_fonte"] for f in novo["fontes"]} == {"B"}

    recibo = db.session.get(AtualizacaoMensalInfraestrutura, "2026-11")
    corredor_b = next(v for v in recibo.resultado["versoes"] if v["resumo_mudancas"])
    assert corredor_b["resumo_mudancas"] == {"GEOMETRIA_OU_ATRIBUTOS_ALTERADOS": 1}

    contagem = AssociacaoInfraestruturaTrecho.query.count()
    assert rodar(outubro.replace(day=20), versao_a) is None and rodar(novembro.replace(day=20), versao_b) is None
    assert AssociacaoInfraestruturaTrecho.query.count() == contagem == 2
    assert len(db.session.get(AtualizacaoMensalInfraestrutura, "2026-11").resultado["classificacoes"]) == 1


def test_mes_da_agenda_segue_o_fuso_de_sao_paulo(app):
    # 2026-12-01T01:00Z ainda é 30/11 22:00 em São Paulo.
    atualizar_mensal(datetime(2026, 12, 1, 1, tzinfo=UTC), lambda _: [])
    assert db.session.get(AtualizacaoMensalInfraestrutura, "2026-11") is not None
    assert db.session.get(AtualizacaoMensalInfraestrutura, "2026-12") is None


def test_sem_rede_gtfs_vigente_nao_classifica_nem_falha(app):
    atualizar_mensal(datetime(2026, 11, 2, tzinfo=UTC), lambda _: [])
    assert registrar_classificacao_vigente(datetime(2026, 11, 2, tzinfo=UTC)) == {
        "motivo": "VersaoGTFSInexistente"}


def test_filtro_de_atributos_exclui_corredor_futuro_e_registra(app):
    """GeoSampa real (26/11/2026) traz corredores 'Futuro'/'Em Implantação' e faixas não exclusivas."""
    dados = fc(feat("1", (-46.60, LAT), (-46.59, LAT), props={"status": "Em Operação"}),
               feat("2", (-46.58, LAT), (-46.57, LAT), props={"status": "Futuro"}),
               feat("3", (-46.58, LAT), (-46.57, LAT), props={"status": "Em Operação"}))
    filtro = {"status": ["Em Operação"]}
    versao = importar(dados, filtro=filtro)
    m = versao.metadados
    assert {c.identificador_fonte for c in ClassificacaoInfraestrutura.query} == {"1", "3"}
    assert m["filtro"] == filtro and m["excluidas_por_filtro"] == {"2": {"status": "Futuro"}}
    assert m["geometrias_duplicadas"] == []
    assert versao.sha256 != m["sha256_arquivo"]          # identidade = arquivo + filtro
    assert importar(dados, filtro=filtro).id == versao.id  # idempotente
    assert importar(dados).id != versao.id                 # sem filtro é outra importação
    with pytest.raises(ValueError, match="nenhuma feição"):
        importar(dados, filtro={"status": ["Desativado"]})


def test_geometria_repetida_com_outro_identificador_fica_registrada(app):
    versao = importar(fc(feat("47", (-46.60, LAT), (-46.59, LAT)), feat("408", (-46.59, LAT), (-46.60, LAT))))
    assert versao.metadados["geometrias_duplicadas"] == [["408", "47"]]  # mesmo traço, ordem inversa


def test_manifesto_aceita_capturada_em_iso_e_filtro(app, tmp_path):
    from app.contexto.infraestrutura.tarefa import atualizar_infraestrutura
    (tmp_path / "c.geojson").write_text(json.dumps(fc(
        feat("1", (-46.60, LAT), (-46.59, LAT), props={"s": "ok"}),
        feat("2", (-46.58, LAT), (-46.57, LAT), props={"s": "Futuro"}))), encoding="utf-8")
    (tmp_path / "m.json").write_text(json.dumps({"bases": [{
        "arquivo": "c.geojson", "fonte": "GeoSampa", "url_fonte": URL, "versao_fonte": "2026-11-26",
        "crs_origem": "EPSG:4326", "categoria": "CORREDOR", "capturada_em": "2026-11-26T17:52:00Z",
        "filtro": {"s": ["ok"]}}]}), encoding="utf-8")
    app.config["INFRAESTRUTURA_SNAPSHOT_MANIFEST"] = str(tmp_path / "m.json")
    resultado = atualizar_infraestrutura(datetime(2026, 11, 27, tzinfo=UTC))
    versao = VersaoInfraestrutura.query.one()
    assert versao.capturada_em == datetime(2026, 11, 26, 17, 52, tzinfo=UTC)
    assert versao.metadados["excluidas_por_filtro"] == {"2": {"s": "Futuro"}}
    assert resultado["classificacao"] == {"motivo": "VersaoGTFSInexistente"}
