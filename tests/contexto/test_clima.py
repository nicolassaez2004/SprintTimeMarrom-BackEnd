"""US06: clima por trecho — modelo, coleta, vínculo espacial e cobertura.

As respostas do INMET aqui seguem o formato documentado da API apitempo
(valores como texto, `null` para ausência, HR_MEDICAO em UTC). São exemplos
sintéticos, não uma coleta real.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pyproj import Transformer
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError

from app import create_app, db
from app.contexto.clima.associacao import associar_trechos_a_estacoes
from app.contexto.clima.coleta import coletar_cge, coletar_clima, coletar_inmet
from app.consultas.clima import cobertura_mensal, consultar_clima_trecho, fins_de_janela
from app.integrations.clima import (
    CGEClient, FonteClimaIndisponivel, INMETClient, ObservacaoFonte,
    converter_estacoes_inmet, converter_observacoes_inmet, instante_inmet,
)
from app.models import (
    ArquivoArmazenado, ColetaClima, EstacaoMeteorologica, Linha, ObservacaoClima, Parada, Trecho,
    TrechoEstacaoClima, VersaoGTFS,
)

UTC = timezone.utc
PARA_METROS = Transformer.from_crs(4326, 31983, always_xy=True)
PARA_GRAUS = Transformer.from_crs(31983, 4326, always_xy=True)


@pytest.fixture
def app(tmp_path):
    application = create_app({
        "TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://", "MYSQL_SSL_CA": None,
        "INMET_TOKEN": "token-teste", "CGE_API_URL": None,
    })
    application.instance_path = str(tmp_path / "instance")
    with application.app_context():
        event.listen(db.engine, "connect",
                     lambda conexao, _: conexao.execute("PRAGMA foreign_keys=ON"))
        db.create_all()
        yield application
        db.session.remove()
        db.engine.dispose()


def _graus(x, y):
    return list(PARA_GRAUS.transform(x, y))


def _rede(trechos_metros, versao_id="gtfs-us06"):
    """Cria uma versão GTFS com trechos dados em metros (EPSG:31983)."""
    agora = datetime(2026, 11, 24, tzinfo=UTC)
    arquivo = ArquivoArmazenado(id=f"arq-{versao_id}", chave=f"gtfs/{versao_id}.zip",
                                nome_original="gtfs.zip", mime_type="application/zip",
                                tamanho_bytes=0, sha256="a" * 64, criado_em=agora)
    versao = VersaoGTFS(id=versao_id, hash="b" * 64, vigencia_inicio=date(2026, 1, 1), arquivo=arquivo)
    linha = Linha(versao_gtfs=versao, codigo="8000-10", nome="Linha de teste")
    db.session.add_all([versao, linha])
    db.session.flush()
    for ordem, (inicio, fim) in enumerate(trechos_metros, start=1):
        for sufixo, ponto in (("o", inicio), ("d", fim)):
            lon, lat = _graus(*ponto)
            db.session.add(Parada(versao_gtfs_id=versao_id, codigo=f"p{ordem}{sufixo}",
                                  nome="Parada", longitude=round(lon, 8), latitude=round(lat, 8)))
        db.session.flush()
        db.session.add(Trecho(
            id=f"trecho-{ordem}", versao_gtfs_id=versao_id, linha_codigo="8000-10", sentido="0",
            trajeto="trip-1", ordem=ordem, parada_origem_codigo=f"p{ordem}o",
            parada_destino_codigo=f"p{ordem}d", geometria=[_graus(*inicio), _graus(*fim)],
        ))
    db.session.commit()
    return versao


def _estacao(codigo, x, y, fonte="INMET", intervalo=3600):
    lon, lat = _graus(x, y)
    estacao = EstacaoMeteorologica(fonte=fonte, codigo=codigo, nome=f"Estação {codigo}",
                                   latitude=round(lat, 8), longitude=round(lon, 8),
                                   intervalo_seg=intervalo, versao_fonte="teste")
    db.session.add(estacao)
    db.session.commit()
    return estacao


X0, Y0 = PARA_METROS.transform(-46.63, -23.55)


# ---------------------------------------------------------------- T01: modelo

def test_modelo_distingue_estacao_medicao_e_vinculo(app):
    tabelas = set(db.metadata.tables)
    assert {"estacoes_meteorologicas", "observacoes_clima", "trechos_estacoes_clima"} <= tabelas
    # Observação pertence à estação; o trecho só se liga à estação pelo vínculo.
    assert {fk.column.table.name for fk in db.metadata.tables["observacoes_clima"].foreign_keys} == {
        "estacoes_meteorologicas", "coletas_clima"}
    assert "trechos" in {fk.column.table.name
                         for fk in db.metadata.tables["trechos_estacoes_clima"].foreign_keys}
    assert "trecho_id" not in db.metadata.tables["observacoes_clima"].columns


def test_faltante_fica_nulo_e_nao_vira_zero(app):
    estacao = _estacao("A701", X0, Y0)
    instante = datetime(2026, 11, 24, 13, tzinfo=UTC)
    db.session.add(ObservacaoClima(estacao_id=estacao.id, instante=instante, intervalo_seg=3600,
                                   chuva=None, temperatura=None, fonte="INMET", versao_fonte="t"))
    db.session.commit()
    obs = ObservacaoClima.query.one()
    assert obs.chuva is None and obs.temperatura is None
    assert (obs.unidade_chuva, obs.unidade_temperatura) == ("mm", "degC")


def test_observacao_nao_se_repete_e_unidade_e_restrita(app):
    estacao = _estacao("A701", X0, Y0)
    instante = datetime(2026, 11, 24, 13, tzinfo=UTC)
    for _ in range(2):
        db.session.add(ObservacaoClima(estacao_id=estacao.id, instante=instante, intervalo_seg=3600,
                                       chuva=Decimal("0"), fonte="INMET", versao_fonte="t"))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()
    db.session.add(ObservacaoClima(estacao_id=estacao.id, instante=instante, intervalo_seg=3600,
                                   chuva=Decimal("1"), unidade_chuva="in", fonte="INMET",
                                   versao_fonte="t"))
    with pytest.raises(IntegrityError):
        db.session.commit()


# --------------------------------------------------------- T02: fonte e coleta

AMOSTRA_INMET = [
    {"CD_ESTACAO": "A701", "DT_MEDICAO": "2026-11-24", "HR_MEDICAO": "1200",
     "CHUVA": "0.0", "TEM_INS": "21.4"},
    {"CD_ESTACAO": "A701", "DT_MEDICAO": "2026-11-24", "HR_MEDICAO": "1300",
     "CHUVA": "2.6", "TEM_INS": "19.8"},
    {"CD_ESTACAO": "A701", "DT_MEDICAO": "2026-11-24", "HR_MEDICAO": "1400",
     "CHUVA": None, "TEM_INS": None},
    {"CD_ESTACAO": "A701", "DT_MEDICAO": "2026-11-24", "HR_MEDICAO": "2300",
     "CHUVA": None, "TEM_INS": None},
]


def test_conversao_inmet_preserva_utc_unidades_e_nulos():
    observacoes = converter_observacoes_inmet(AMOSTRA_INMET, "A701")
    assert observacoes[1].instante == datetime(2026, 11, 24, 13, tzinfo=UTC)
    assert observacoes[0].chuva_mm == Decimal("0.0")  # zero publicado continua zero
    assert observacoes[1].chuva_mm == Decimal("2.6") and observacoes[1].temperatura_c == Decimal("19.8")
    assert observacoes[2].chuva_mm is None and observacoes[2].temperatura_c is None
    assert instante_inmet("2026-11-24", "0") == datetime(2026, 11, 24, 0, tzinfo=UTC)
    with pytest.raises(FonteClimaIndisponivel):
        converter_observacoes_inmet([{**AMOSTRA_INMET[0], "CD_ESTACAO": "A771"}], "A701")
    with pytest.raises(FonteClimaIndisponivel):
        instante_inmet("2026-11-24", "25h")


def test_catalogo_inmet_ignora_estacao_sem_coordenadas():
    estacoes = converter_estacoes_inmet([
        {"CD_ESTACAO": "A701", "DC_NOME": "SAO PAULO - MIRANTE", "VL_LATITUDE": "-23.49638888",
         "VL_LONGITUDE": "-46.61999999", "VL_ALTITUDE": "785.64", "SG_ESTADO": "SP",
         "CD_SITUACAO": "Operante", "DT_INICIO_OPERACAO": "2006-07-25T21:00:00.000-03:00"},
        {"CD_ESTACAO": "X000", "DC_NOME": "SEM POSICAO", "VL_LATITUDE": None, "VL_LONGITUDE": None},
    ])
    assert [e.codigo for e in estacoes] == ["A701"]
    assert estacoes[0].operacao_inicio == date(2006, 7, 25)


def test_cliente_inmet_exige_token_e_nao_expoe_url():
    with pytest.raises(FonteClimaIndisponivel, match="INMET_TOKEN"):
        INMETClient("https://exemplo.invalido").observacoes_horarias(
            "A701", date(2026, 11, 24), date(2026, 11, 24))

    class SessaoFalha:
        def get(self, url, timeout):
            import requests
            raise requests.ConnectionError(url)

    with pytest.raises(FonteClimaIndisponivel) as erro:
        INMETClient("https://exemplo.invalido", "segredo", sessao=SessaoFalha()).observacoes_horarias(
            "A701", date(2026, 11, 24), date(2026, 11, 24))
    assert "segredo" not in str(erro.value)


class ClienteFalso:
    def __init__(self, respostas):
        self.respostas = respostas
        self.chamadas = []

    def observacoes_horarias(self, codigo, inicio, fim):
        self.chamadas.append((codigo, inicio, fim))
        resposta = self.respostas[codigo]
        if isinstance(resposta, Exception):
            raise resposta
        return converter_observacoes_inmet(resposta, codigo)


def test_coleta_inmet_grava_sem_repetir_e_preenche_nulos_atrasados(app):
    estacao = _estacao("A701", X0, Y0)
    agora = datetime(2026, 11, 24, 15, 5, tzinfo=UTC)
    coleta = coletar_inmet(agora, cliente=ClienteFalso({"A701": AMOSTRA_INMET}), agora=agora)
    assert coleta.estado == "OK"
    assert (coleta.recebidas, coleta.novas) == (3, 3)  # 23h ainda não ocorreu
    assert ObservacaoClima.query.count() == 3

    tardia = [*AMOSTRA_INMET[:2], {**AMOSTRA_INMET[2], "CHUVA": "0.4", "TEM_INS": "18.9"}]
    segunda = coletar_inmet(agora, cliente=ClienteFalso({"A701": tardia}), agora=agora)
    assert segunda.novas == 0 and ObservacaoClima.query.count() == 3
    preenchida = ObservacaoClima.query.filter_by(
        estacao_id=estacao.id, instante=datetime(2026, 11, 24, 14, tzinfo=UTC)).one()
    assert preenchida.chuva == Decimal("0.4")


def test_coleta_parcial_e_indisponibilidade_ficam_registradas(app):
    _estacao("A701", X0, Y0)
    _estacao("A771", X0 + 1000, Y0)
    agora = datetime(2026, 11, 24, 15, 5, tzinfo=UTC)
    parcial = coletar_inmet(agora, agora=agora, cliente=ClienteFalso({
        "A701": AMOSTRA_INMET, "A771": FonteClimaIndisponivel("INMET indisponível (Timeout)")}))
    assert parcial.estado == "PARCIAL" and "Timeout" in parcial.erro
    sem_token = coletar_inmet(agora, agora=agora, cliente=ClienteFalso({
        "A701": FonteClimaIndisponivel("INMET_TOKEN não configurado"),
        "A771": FonteClimaIndisponivel("INMET_TOKEN não configurado")}))
    assert sem_token.estado == "INDISPONIVEL"


def test_cge_sem_acesso_registra_indisponivel_sem_bloquear_inmet(app, monkeypatch):
    _estacao("A701", X0, Y0)
    agora = datetime(2026, 11, 24, 15, 5, tzinfo=UTC)
    cge = coletar_cge(agora, cliente=CGEClient(None), agora=agora)
    assert cge.estado == "INDISPONIVEL" and "CGE" in cge.erro

    from app.contexto.clima import coleta as modulo
    monkeypatch.setattr(modulo, "cliente_inmet", lambda: ClienteFalso({"A701": AMOSTRA_INMET}))
    monkeypatch.setattr(modulo, "sincronizar_estacoes_inmet", lambda **_: (0, False))
    db.session.query(ColetaClima).delete()
    db.session.commit()
    coletar_clima(agora)
    estados = {c.fonte: c.estado for c in ColetaClima.query}
    assert estados == {"INMET": "OK", "CGE": "INDISPONIVEL"}
    # Mesma hora: não repete a coleta INMET; a grade do CGE é de 15 min.
    coletar_clima(agora + timedelta(minutes=1))
    assert ColetaClima.query.filter_by(fonte="INMET").count() == 1
    coletar_clima(agora + timedelta(minutes=11))
    assert ColetaClima.query.filter_by(fonte="CGE").count() == 2


# ------------------------------------------------------- T03: vínculo espacial

def test_cada_trecho_recebe_o_posto_mais_proximo_ou_bloqueio(app):
    _rede([
        ((X0, Y0), (X0 + 500, Y0)),                     # perto da A701
        ((X0 + 20_000, Y0), (X0 + 20_500, Y0)),         # perto da A771
        ((X0 + 80_000, Y0), (X0 + 80_500, Y0)),         # longe de todas
    ])
    _estacao("A701", X0 + 250, Y0 + 300)
    _estacao("A771", X0 + 20_250, Y0 - 1_000)
    resultado = associar_trechos_a_estacoes("gtfs-us06", raio_maximo_m=25_000)
    assert resultado.identificados_pct == 100.0
    assert (resultado.associados, resultado.fora_do_raio, resultado.sem_estacao) == (2, 1, 0)
    vinculos = {v.trecho_id: v for v in TrechoEstacaoClima.query}
    assert vinculos["trecho-1"].estacao.codigo == "A701"
    assert vinculos["trecho-1"].distancia_m == pytest.approx(300, abs=1)
    assert vinculos["trecho-2"].estacao.codigo == "A771"
    assert vinculos["trecho-2"].distancia_m == pytest.approx(1_000, abs=1)
    assert vinculos["trecho-3"].situacao == "FORA_DO_RAIO"
    assert vinculos["trecho-3"].estacao.codigo == "A771"  # registrada, mas não usada
    assert {v.crs_medicao for v in vinculos.values()} == {"EPSG:31983"}


def test_sem_estacao_e_recalculo_sem_duplicar(app):
    _rede([((X0, Y0), (X0 + 500, Y0))])
    sem = associar_trechos_a_estacoes("gtfs-us06")
    assert sem.sem_estacao == 1 and sem.identificados_pct == 100.0
    assert TrechoEstacaoClima.query.one().estacao_id is None
    _estacao("A771", X0, Y0 + 400)
    _estacao("A701", X0, Y0 + 400)  # empate: menor código vence
    associar_trechos_a_estacoes("gtfs-us06")
    vinculo = TrechoEstacaoClima.query.one()
    assert vinculo.estacao.codigo == "A701" and vinculo.situacao == "ASSOCIADO"


# ------------------------------------------- T03/T04: janelas, consulta e meta

def _obs(estacao, hora, chuva, temperatura, dia=24):
    db.session.add(ObservacaoClima(
        estacao_id=estacao.id, instante=datetime(2026, 11, dia, hora, tzinfo=UTC), intervalo_seg=3600,
        chuva=chuva, temperatura=temperatura, fonte="INMET", versao_fonte="t"))


def test_janelas_sao_inteiras_e_alinhadas():
    fins = list(fins_de_janela(datetime(2026, 11, 24, 10, 30, tzinfo=UTC),
                               datetime(2026, 11, 24, 13, 0, tzinfo=UTC), 3600))
    assert fins == [datetime(2026, 11, 24, 12, tzinfo=UTC), datetime(2026, 11, 24, 13, tzinfo=UTC)]


def test_consulta_vincula_janelas_sem_inventar_valor(app):
    _rede([((X0, Y0), (X0 + 500, Y0))])
    estacao = _estacao("A701", X0, Y0 + 200)
    associar_trechos_a_estacoes("gtfs-us06")
    _obs(estacao, 11, Decimal("0"), Decimal("22.1"))
    _obs(estacao, 12, Decimal("3.2"), None)
    db.session.commit()
    corpo = consultar_clima_trecho("trecho-1", datetime(2026, 11, 24, 10, tzinfo=UTC),
                                   datetime(2026, 11, 24, 13, tzinfo=UTC),
                                   agora=datetime(2026, 11, 25, tzinfo=UTC))
    janelas = corpo["janelas"]
    assert [j["fim"] for j in janelas] == ["2026-11-24T11:00:00Z", "2026-11-24T12:00:00Z",
                                            "2026-11-24T13:00:00Z"]
    assert janelas[0]["chuva_mm"] == 0.0 and janelas[0]["motivo"] is None
    assert janelas[1]["temperatura_c"] is None and janelas[1]["motivo"] == "TEMPERATURA_NAO_INFORMADA"
    assert janelas[2]["chuva_mm"] is None and janelas[2]["motivo"] == "SEM_OBSERVACAO"
    assert corpo["cobertura"]["chuva_pct"] == pytest.approx(66.67)
    assert corpo["unidades"] == {"chuva": "mm", "temperatura": "°C"}
    assert corpo["estacao"]["codigo"] == "A701"


def test_trecho_fora_do_raio_nao_recebe_medicao(app):
    _rede([((X0, Y0), (X0 + 500, Y0))])
    estacao = _estacao("A701", X0 + 50_000, Y0)
    associar_trechos_a_estacoes("gtfs-us06", raio_maximo_m=25_000)
    _obs(estacao, 11, Decimal("5"), Decimal("20"))
    db.session.commit()
    corpo = consultar_clima_trecho("trecho-1", datetime(2026, 11, 24, 10, tzinfo=UTC),
                                   datetime(2026, 11, 24, 11, tzinfo=UTC),
                                   agora=datetime(2026, 11, 25, tzinfo=UTC))
    assert corpo["janelas"][0] == {"inicio": "2026-11-24T10:00:00Z", "fim": "2026-11-24T11:00:00Z",
                                   "chuva_mm": None, "temperatura_c": None, "motivo": "FORA_DO_RAIO"}


def test_cobertura_mensal_mede_meta_e_marca_amostra(app):
    _rede([((X0, Y0), (X0 + 500, Y0))])
    estacao = _estacao("A701", X0, Y0 + 200)
    associar_trechos_a_estacoes("gtfs-us06")
    # Novembro/2026 em São Paulo (UTC-3): 01/11 03:00Z a 01/12 03:00Z, 720 horas.
    inicio = datetime(2026, 11, 1, 3, tzinfo=UTC)
    for hora in range(720):
        if hora in (5, 6):  # duas horas sem medição
            continue
        instante = inicio + timedelta(hours=hora + 1)
        db.session.add(ObservacaoClima(estacao_id=estacao.id, instante=instante, intervalo_seg=3600,
                                       chuva=Decimal("0"), temperatura=Decimal("20"),
                                       fonte="INMET", versao_fonte="t"))
    db.session.add(ColetaClima(fonte="INMET", prevista_em=inicio, iniciada_em=inicio, estado="OK"))
    db.session.commit()

    mensal = cobertura_mensal(2026, 11, agora=datetime(2026, 12, 2, tzinfo=UTC))
    resultado = mensal["estacoes"][0]
    assert resultado["resultado"] == "MENSAL"
    assert resultado["janelas_esperadas"] == 720 and resultado["chuva_recebidas"] == 718
    assert resultado["chuva_pct"] == pytest.approx(99.72) and resultado["atinge_meta"] is True
    assert resultado["sem_medicao"] == ["2026-11-01T09:00:00Z", "2026-11-01T10:00:00Z"]
    assert mensal["coletas_por_estado"] == {"OK": 1}

    parcial = cobertura_mensal(2026, 11, agora=datetime(2026, 11, 10, tzinfo=UTC))
    assert parcial["estacoes"][0]["resultado"] == "AMOSTRA"


# ---------------------------------------------------------------- T04: rota

def test_rota_contexto_clima(app):
    _rede([((X0, Y0), (X0 + 500, Y0))])
    estacao = _estacao("A701", X0, Y0 + 200)
    associar_trechos_a_estacoes("gtfs-us06")
    _obs(estacao, 11, Decimal("1.2"), Decimal("20.5"))
    db.session.commit()
    cliente = app.test_client()
    resposta = cliente.get("/contexto/clima?trecho_id=trecho-1"
                           "&inicio=2026-11-24T10:00:00Z&fim=2026-11-24T11:00:00Z")
    assert resposta.status_code == 200
    assert resposta.get_json()["janelas"][0]["chuva_mm"] == 1.2
    assert cliente.get("/contexto/clima?trecho_id=nao-existe"
                       "&inicio=2026-11-24T10:00:00Z&fim=2026-11-24T11:00:00Z").status_code == 404
    for consulta in ("trecho_id=trecho-1&inicio=2026-11-24&fim=2026-11-25",
                     "trecho_id=trecho-1&inicio=2026-11-01T00:00:00Z&fim=2026-12-05T00:00:00Z",
                     "trecho_id=trecho-1&inicio=2026-11-24T10:00:00Z&fim=2026-11-24T11:00:00Z&x=1",
                     "trecho_id=trecho-1&inicio=2026-11-24T10:00:00Z&fim=2026-11-24T11:00:00Z&fonte=OUTRA"):
        assert cliente.get("/contexto/clima?" + consulta).status_code == 400, consulta


# ----------------------------------------------------- revisão US06 (26/11/2026)

def test_sentinela_e_valores_impossiveis_viram_nulo_e_nao_derrubam_a_coleta(app):
    estacao = _estacao("A701", X0, Y0)
    resposta = [
        {**AMOSTRA_INMET[0], "CHUVA": "-9999", "TEM_INS": "-9999"},
        {**AMOSTRA_INMET[1], "CHUVA": "-0.2", "TEM_INS": "71.0"},   # chuva negativa violaria o CHECK
    ]
    convertidas = converter_observacoes_inmet(resposta, "A701")
    assert [(o.chuva_mm, o.temperatura_c, o.descartados) for o in convertidas] == [
        (None, None, ("chuva", "temperatura")), (None, None, ("chuva", "temperatura"))]
    agora = datetime(2026, 11, 24, 15, 5, tzinfo=UTC)
    coleta = coletar_inmet(agora, cliente=ClienteFalso({"A701": resposta}), agora=agora)
    assert coleta.estado == "OK" and coleta.novas == 2
    assert {(o.chuva, o.temperatura) for o in ObservacaoClima.query.filter_by(estacao_id=estacao.id)} == {(None, None)}


def test_catalogo_ignora_coordenadas_fora_de_wgs84_ou_zeradas():
    base = {"DC_NOME": "X", "SG_ESTADO": "SP"}
    estacoes = converter_estacoes_inmet([
        {**base, "CD_ESTACAO": "A1", "VL_LATITUDE": "-123.5", "VL_LONGITUDE": "-46.6"},
        {**base, "CD_ESTACAO": "A2", "VL_LATITUDE": "0", "VL_LONGITUDE": "0"},
        {**base, "CD_ESTACAO": "A701", "VL_LATITUDE": "-23.49", "VL_LONGITUDE": "-46.62"},
    ])
    assert [e.codigo for e in estacoes] == ["A701"]


def test_erro_inesperado_encerra_a_tentativa_como_falha(app, monkeypatch):
    _estacao("A701", X0, Y0)
    from app.contexto.clima import coleta as modulo

    def quebra(*_):
        raise RuntimeError("banco caiu")

    monkeypatch.setattr(modulo, "_gravar_observacao", quebra)
    agora = datetime(2026, 11, 24, 15, 5, tzinfo=UTC)
    with pytest.raises(RuntimeError):
        coletar_inmet(agora, cliente=ClienteFalso({"A701": AMOSTRA_INMET}), agora=agora)
    tentativa = ColetaClima.query.one()
    assert tentativa.estado == "FALHA" and "RuntimeError" in tentativa.erro
    assert tentativa.finalizada_em is not None


def test_trecho_sem_geometria_suficiente_tem_impedimento_explicito(app):
    _rede([((X0, Y0), (X0 + 500, Y0)), ((X0, Y0), (X0 + 900, Y0)), ((X0, Y0), (X0 + 700, Y0))])
    ponto = _graus(X0, Y0)
    db.session.get(Trecho, "trecho-2").geometria = [ponto, ponto]            # um único ponto repetido
    db.session.get(Trecho, "trecho-3").geometria = [[0.0, 0.0], ponto]       # coordenada nula
    db.session.commit()
    _estacao("A701", X0, Y0 + 200)
    resultado = associar_trechos_a_estacoes("gtfs-us06")
    assert (resultado.associados, resultado.geometria_invalida, resultado.identificados_pct) == (1, 2, 100.0)
    invalido = db.session.get(TrechoEstacaoClima, ("trecho-2", "INMET"))
    assert (invalido.situacao, invalido.estacao_id, invalido.distancia_m) == ("GEOMETRIA_INVALIDA", None, None)
    corpo = consultar_clima_trecho("trecho-2", datetime(2026, 11, 24, 10, tzinfo=UTC),
                                   datetime(2026, 11, 24, 11, tzinfo=UTC), agora=datetime(2026, 11, 25, tzinfo=UTC))
    assert corpo["janelas"][0]["motivo"] == "GEOMETRIA_INVALIDA" and corpo["janelas"][0]["chuva_mm"] is None


def test_distancias_conferidas_por_geodesica_independente(app):
    """Amostra manual: distância UTM 23S x distância geodésica (elipsoide WGS84) ao segmento."""
    from pyproj import Geod
    _rede([((X0, Y0), (X0 + 500, Y0)), ((X0 + 3000, Y0 + 4000), (X0 + 3000, Y0 + 9000))])
    estacao = _estacao("A701", X0 + 250, Y0 + 1200)
    associar_trechos_a_estacoes("gtfs-us06")
    geod = Geod(ellps="WGS84")
    lon_e, lat_e = float(estacao.longitude), float(estacao.latitude)
    for trecho_id in ("trecho-1", "trecho-2"):
        trecho = db.session.get(Trecho, trecho_id)
        (lon1, lat1), (lon2, lat2) = trecho.geometria
        amostras = [(lon1 + (lon2 - lon1) * k / 2000, lat1 + (lat2 - lat1) * k / 2000) for k in range(2001)]
        geodesica = min(geod.inv(lon_e, lat_e, lon, lat)[2] for lon, lat in amostras)
        calculada = db.session.get(TrechoEstacaoClima, (trecho_id, "INMET")).distancia_m
        assert calculada == pytest.approx(geodesica, rel=0.002), trecho_id   # escala UTM < 0,2%


def test_mudanca_no_catalogo_recalcula_o_vinculo(app, monkeypatch):
    _rede([((X0, Y0), (X0 + 500, Y0))])
    _estacao("A771", X0, Y0 + 5000)
    associar_trechos_a_estacoes("gtfs-us06")
    assert TrechoEstacaoClima.query.one().estacao.codigo == "A771"
    lon, lat = _graus(X0 + 250, Y0 + 100)

    class Catalogo:
        def listar_estacoes_automaticas(self):
            return converter_estacoes_inmet([
                {"CD_ESTACAO": "A771", "VL_LATITUDE": str(EstacaoMeteorologica.query.one().latitude),
                 "VL_LONGITUDE": str(EstacaoMeteorologica.query.one().longitude)},
                {"CD_ESTACAO": "A701", "VL_LATITUDE": f"{lat:.8f}", "VL_LONGITUDE": f"{lon:.8f}"}])

    from app.contexto.clima import coleta as modulo
    monkeypatch.setattr(modulo, "cliente_inmet", lambda: Catalogo())
    monkeypatch.setattr(modulo, "coletar_inmet", lambda *a, **k: None)
    monkeypatch.setattr(modulo, "coletar_cge", lambda *a, **k: None)
    agora = datetime(2026, 11, 24, 15, 5, tzinfo=UTC)
    EstacaoMeteorologica.query.update({"atualizada_em": agora - timedelta(days=2)})
    db.session.commit()
    coletar_clima(agora)
    vinculo = TrechoEstacaoClima.query.one()
    assert vinculo.estacao.codigo == "A701" and vinculo.distancia_m == pytest.approx(100, abs=1)
    # Catálogo sem mudança não recalcula: calculado_em continua o mesmo.
    antes = vinculo.calculado_em
    EstacaoMeteorologica.query.update({"atualizada_em": agora - timedelta(days=2)})
    db.session.commit()
    coletar_clima(agora + timedelta(hours=1))
    assert TrechoEstacaoClima.query.one().calculado_em == antes


def test_cobertura_nao_conta_repeticao_nem_instante_fora_da_grade(app):
    _rede([((X0, Y0), (X0 + 500, Y0))])
    estacao = _estacao("A701", X0, Y0 + 200)
    associar_trechos_a_estacoes("gtfs-us06")
    _obs(estacao, 11, Decimal("0"), Decimal("20"))
    db.session.add(ObservacaoClima(estacao_id=estacao.id, instante=datetime(2026, 11, 24, 11, 30, tzinfo=UTC),
                                   intervalo_seg=3600, chuva=Decimal("1"), temperatura=Decimal("20"),
                                   fonte="INMET", versao_fonte="t"))
    db.session.commit()
    corpo = consultar_clima_trecho("trecho-1", datetime(2026, 11, 24, 10, tzinfo=UTC),
                                   datetime(2026, 11, 24, 12, tzinfo=UTC), agora=datetime(2026, 11, 25, tzinfo=UTC))
    c = corpo["cobertura"]
    assert (c["janelas_esperadas"], c["chuva_recebidas"], c["janelas_sem_observacao"]) == (2, 1, 1)
    mensal = cobertura_mensal(2026, 11, agora=datetime(2026, 11, 25, tzinfo=UTC))
    assert "não são descontados" in mensal["denominador"] and mensal["limitacoes"]
