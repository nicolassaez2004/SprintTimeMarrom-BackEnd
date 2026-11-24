"""US23: contrato real HTTP, persistência local e dados sintéticos."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event
from sqlalchemy.dialects import mysql

from app import create_app, db
from app.indicadores.series import _ou_balanceado
from app.models import ExecucaoColeta, IndicadorOperacional as I, LimiteConsultaIP

UTC = timezone.utc
D0 = datetime(2026, 11, 24, 3, tzinfo=UTC)  # meia-noite em São Paulo
VERSAO = "00000000-0000-0000-0000-000000000001"


@pytest.fixture
def cliente():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://",
                      "MYSQL_SSL_CA": None, "LIMITES_RELOGIO": lambda: 120})
    with app.app_context():
        db.metadata.create_all(db.engine, tables=[I.__table__, LimiteConsultaIP.__table__, ExecucaoColeta.__table__])
        yield app.test_client()
        db.session.remove()
        db.engine.dispose()


def gravar(hora=8, *, duracao=1, valor=80, **campos):
    registro = I(linha_codigo="8000-10", sentido="0", indicador="PONTUALIDADE_VIAGENS",
                 valor=valor, unidade="%", periodo_inicio=D0 + timedelta(hours=hora),
                 periodo_fim=D0 + timedelta(hours=hora + duracao), versao_gtfs_id=VERSAO,
                 versao_conjunto=f"conjunto-{hora}", versao_correspondencia="corr-1",
                 versao_calculo="pontualidade-1", qualidade={"versao_reconstrucao": "recon-1"})
    for campo, valor_campo in campos.items():
        setattr(registro, campo, valor_campo)
    db.session.add(registro)
    db.session.commit()
    return registro


def consultar(cliente, **filtros):
    resposta = cliente.get("/series", query_string={"linha": "8000-10", "sentido": "ida", **filtros})
    assert resposta.status_code == 200, resposta.get_json()
    return resposta.get_json()


def test_zero_null_e_lacuna_tem_significados_distintos(cliente):
    gravar(8, valor=0)
    gravar(10, valor=None)
    corpo = consultar(cliente)
    assert [(p["valor"], p["status"]) for p in corpo["dados"]] == [(0, "DISPONIVEL"), (None, "SEM_RESULTADO")]
    assert [(l["inicio"], l["fim"], l["motivo"]) for l in corpo["lacunas"]] == [
        ((D0 + timedelta(hours=9)).isoformat(), (D0 + timedelta(hours=10)).isoformat(), "SEM_PUBLICACAO"),
        ((D0 + timedelta(hours=10)).isoformat(), (D0 + timedelta(hours=11)).isoformat(), "RESULTADO_NULO"),
    ]
    assert corpo["dados"][0]["unidade"] == "%"
    assert corpo["dados"][0]["versao_gtfs_id"] == VERSAO
    assert corpo["dados"][0]["qualidade"]["versao_reconstrucao"] == "recon-1"


def test_periodo_nao_retém_agregado_que_ultrapassa_limite(cliente):
    gravar(7, duracao=2)
    gravar(9)
    gravar(10)
    corpo = consultar(cliente, inicio=(D0 + timedelta(hours=8)).isoformat(), fim=(D0 + timedelta(hours=10)).isoformat())
    assert [p["inicio"] for p in corpo["dados"]] == [(D0 + timedelta(hours=9)).isoformat()]
    assert corpo["paginacao"]["total"] == 1


def test_faixa_diaria_em_sao_paulo_nao_utc_e_sem_fatiar_agregado(cliente):
    for hora in (5, 7, 8, 24 + 7):
        gravar(hora)
    gravar(0, duracao=24)
    corpo = consultar(cliente, hora_inicio="06:00", hora_fim="08:00")
    assert [p["inicio"] for p in corpo["dados"]] == [
        (D0 + timedelta(hours=7)).isoformat(), (D0 + timedelta(hours=31)).isoformat(),
    ]
    assert corpo["consulta"]["fuso"] == "America/Sao_Paulo"
    assert all(l["inicio"][11:16] == "09:00" for l in corpo["lacunas"])


def test_faixa_noturna_cruza_meia_noite(cliente):
    gravar(23)
    gravar(24)
    gravar(26)
    corpo = consultar(cliente, hora_inicio="22:00", hora_fim="02:00")
    assert len(corpo["dados"]) == 2


def test_filtros_isolam_linha_sentido_tipo_rede_e_dimensao(cliente):
    gravar()
    gravar(linha_codigo="outra")
    gravar(sentido="1")
    gravar(indicador="CUMPRIMENTO_VIAGENS")
    gravar(versao_gtfs_id="00000000-0000-0000-0000-000000000002")
    gravar(parada_codigo="p1")
    corpo = consultar(cliente, tipo="PONTUALIDADE_VIAGENS", versao_gtfs_id=VERSAO)
    assert len(corpo["dados"]) == 1


def test_historico_remonta_a_coleta_mesmo_sem_publicacao_inicial(cliente):
    db.session.add(ExecucaoColeta(iniciada_em=D0, estado="OK"))
    db.session.commit()
    gravar(8)
    corpo = consultar(cliente)
    assert corpo["historico"]["coleta_inicio"] == D0.isoformat()
    assert corpo["historico"]["inicio_indicadores"] == (D0 + timedelta(hours=8)).isoformat()
    assert corpo["consulta"]["inicio"] == D0.isoformat()
    assert corpo["lacunas"][0]["inicio"] == D0.isoformat()


def test_lacunas_entre_paginas_e_sobreposicao_nao_inventam_ausencia(cliente):
    gravar(7, duracao=5)
    gravar(8)
    gravar(11)
    corpo = consultar(cliente, page=2, page_size=1)
    assert corpo["paginacao"]["total"] == 3
    assert corpo["lacunas"] == []
    assert consultar(cliente, page=3, page_size=1)["lacunas"] == []
    assert consultar(cliente, page=4, page_size=1)["lacunas"] == []


def test_sem_publicacao_sem_historico_e_periodo_fora_da_base(cliente):
    assert consultar(cliente)["historico"]["inicio_indicadores"] is None
    corpo = consultar(cliente, inicio=D0.isoformat(), fim=(D0 + timedelta(days=1)).isoformat())
    assert corpo["dados"] == [] and len(corpo["lacunas"]) == 1
    gravar()
    assert consultar(cliente, inicio=(D0 + timedelta(days=1)).isoformat())["dados"] == []


def test_query_de_horas_nao_explode_com_periodo_de_seculos_sem_dados(cliente):
    gravar()
    corpo = consultar(cliente, inicio="1900-01-01T00:00:00Z", fim="2100-01-01T00:00:00Z",
                      hora_inicio="06:00", hora_fim="10:00")
    assert corpo["paginacao"]["total"] == 1
    assert len(corpo["lacunas"]) == 200
    assert corpo["consulta"]["lacunas_truncadas"] is True


@pytest.mark.parametrize("query", [
    "hora_inicio=08:00", "hora_inicio=25:00&hora_fim=09:00", "hora_inicio=8:00&hora_fim=09:00",
    "hora_inicio=08:00&hora_fim=08:00", "hora_inicio=08:00&hora_fim=09:00&hora_inicio=07:00",
    "inicio=2026-11-24T00:00:00Z&fim=2026-11-24T00:00:00Z",
])
def test_filtros_invalidos_retornam_400(cliente, query):
    resposta = cliente.get("/series?" + query)
    assert resposta.status_code == 400
    assert resposta.get_json()["erro"]["codigo"] == "PARAMETRO_INVALIDO"


def test_rota_so_le_indicadores_e_historico_sem_executar_calculo(cliente):
    gravar()
    comandos = []
    def capturar(_, __, sql, *args):
        comandos.append(sql.lower())
    event.listen(db.engine, "before_cursor_execute", capturar)
    try:
        consultar(cliente)
    finally:
        event.remove(db.engine, "before_cursor_execute", capturar)
    assert not any("registros_gps" in sql or "viagens_observadas" in sql for sql in comandos)
    assert all(sql.lstrip().startswith("select") for sql in comandos if "limites_consultas_ip" not in sql)
    assert "over (partition by" in " ".join(comandos)
    assert db.session.query(I).count() == 1


def test_predicado_horario_compila_no_mysql():
    predicado = _ou_balanceado([I.periodo_inicio >= D0 + timedelta(days=n) for n in range(120)])
    assert "indicadores_operacionais.periodo_inicio" in str(predicado.compile(dialect=mysql.dialect()))


def test_base_descartavel_do_ensaio_us11_continua_compativel_com_series(tmp_path):
    from scripts.medir_api_indicadores import _preparar_base
    caminho = tmp_path / "ensaio.sqlite"
    _preparar_base(caminho, 4)
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///" + caminho.as_posix(),
                      "MYSQL_SSL_CA": None})
    try:
        resposta = app.test_client().get("/series?linha=8000")
        assert resposta.status_code == 200
        assert resposta.get_json()["paginacao"]["total"] == 2
    finally:
        with app.app_context():
            db.session.remove()
            db.engine.dispose()
