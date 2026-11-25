"""Contrato HTTP e leitura de programação persistida, em SQLite isolado."""

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import pytest
import requests
from sqlalchemy import event
from sqlalchemy.exc import OperationalError

from app import db
from app.models import ArquivoArmazenado, ImportacaoProgramacao, Linha, PartidaProgramada, VersaoGTFS
from app.api import partidas as routes
from app.programacao import service
from app.programacao.gtfs import OfertaGTFS
from app.programacao.normalizacao import ProgramacaoNormalizada
from app.repositories import programacao as repositorio

DATA = date(2026, 11, 19)
AGORA = datetime(2026, 11, 19, 12, tzinfo=timezone.utc)
FILTROS = {"linha": "3762-10", "sentido": "ida", "data": "2026-11-19"}


def _get(app, **filtros):
    return app.test_client().get("/partidas", query_string={**FILTROS, **filtros})


def _conforme_contrato(dados):
    """Regras condicionais do JSON Schema (if/then), sem depender de jsonschema."""
    if dados["estado"] == "DISPONIVEL":
        assert dados["quantidade"] == len(dados["partidas"]) >= 1
    else:
        assert dados["quantidade"] == 0 and dados["partidas"] == []
    if dados["estado"] == "NAO_IMPORTADA":
        assert dados["procedencia"] is None
    else:
        procedencia = dados["procedencia"]
        assert isinstance(dados["tipo_dia"], str)
        oficial = procedencia["fonte"] == "OFICIAL"
        assert isinstance(procedencia["url"], str) is oficial
        assert isinstance(procedencia["referencia"], str) is not oficial
    for partida in dados["partidas"]:
        assert (partida["intervalo_seg"] is None) is (partida["tipo_oferta"] == "FIXA")


def _oficial(fabrica_captura, horarios=("05:00", "23:50", "00:10"), **kwargs):
    return service.importar_oficial("3762-10", "ida", DATA, "UTIL",
                                    captura=fabrica_captura(horarios), agora=AGORA, **kwargs)


def _gtfs(versao, *, tipo_dia="UTIL", codigo="3762-10", agora=AGORA):
    grade = ProgramacaoNormalizada(codigo, "0", DATA, tipo_dia, (
        OfertaGTFS(AGORA, "FIXA"), OfertaGTFS(AGORA + timedelta(minutes=10), "FREQUENCIA", 600),
    ))
    return repositorio.guardar(grade, versao.id, versao.hash, versao.arquivo.chave,
                               f"urn:sha256:{versao.hash}", 0, agora, fonte="GTFS",
                               arquivo_existente=versao.arquivo)[0]


def test_consulta_oficial_com_procedencia_utc_e_alias(app, versao, fabrica_captura):
    importada = _oficial(fabrica_captura)
    resposta = _get(app)
    assert resposta.status_code == 200 and resposta.is_json
    dados = resposta.get_json()
    assert dados["estado"] == "DISPONIVEL" and dados["quantidade"] == 3
    assert dados["procedencia"] == importada["procedencia"]
    assert dados["partidas"] == importada["partidas"]
    assert dados["data_operacao"] == "2026-11-19" and dados["sentido"] == "0"
    assert dados["partidas"][-1]["saida_prevista"] == "2026-11-20T03:10:00+00:00"
    alias = app.test_client().get("/api/partidas", query_string=FILTROS)
    assert alias.status_code == 200 and alias.get_json() == dados
    schema = json.loads((Path(__file__).resolve().parents[2] /
                         "docs/schemas/partidas-resposta.schema.json").read_text(encoding="utf-8"))
    assert set(dados) == set(schema["$defs"]["ConsultaPartidas"]["required"])
    assert set(dados["procedencia"]) == set(schema["$defs"]["Procedencia"]["required"])
    assert set(dados["partidas"][0]) == set(schema["$defs"]["Partida"]["required"])
    _conforme_contrato(dados)


def test_oferta_gtfs_fixa_e_por_frequencia_mantem_tipo_intervalo_e_versao(app, versao):
    _gtfs(versao)
    resposta = _get(app)
    assert resposta.status_code == 200
    dados = resposta.get_json()
    assert dados["procedencia"]["fonte"] == "GTFS"
    assert dados["procedencia"]["versao_fonte"] == versao.hash
    assert dados["procedencia"]["versao_gtfs_id"] == versao.id
    assert dados["procedencia"]["url"] is None
    assert dados["procedencia"]["referencia"] == f"urn:sha256:{versao.hash}"
    assert [(p["tipo_oferta"], p["intervalo_seg"]) for p in dados["partidas"]] == [
        ("FIXA", None), ("FREQUENCIA", 600),
    ]
    _conforme_contrato(dados)


@pytest.mark.parametrize("horarios", [(), ("05:00",)])
def test_oficial_tem_prioridade_sobre_gtfs_mais_recente_inclusive_grade_vazia(
    app, versao, fabrica_captura, horarios,
):
    _oficial(fabrica_captura, horarios)
    _gtfs(versao, agora=AGORA + timedelta(hours=1))
    dados = _get(app).get_json()
    assert dados["procedencia"]["fonte"] == "OFICIAL"
    assert dados["quantidade"] == len(horarios)


def test_lista_vazia_valida_e_ausencia_de_importacao_tem_estados_distintos(app, versao, fabrica_captura):
    sem_importacao = _get(app)
    assert sem_importacao.status_code == 200
    assert sem_importacao.get_json()["estado"] == "NAO_IMPORTADA"
    assert sem_importacao.get_json()["procedencia"] is None
    _conforme_contrato(sem_importacao.get_json())
    _oficial(fabrica_captura, ())
    sem_partidas = _get(app)
    assert sem_partidas.status_code == 200
    dados = sem_partidas.get_json()
    assert dados["estado"] == "SEM_PARTIDAS" and dados["procedencia"]["fonte"] == "OFICIAL"
    assert dados["partidas"] == [] and dados["quantidade"] == 0
    _conforme_contrato(dados)


@pytest.mark.parametrize("filtros", [{"sentido": "volta"}, {"data": "2026-11-20"},
                                     {"tipo_dia": "SABADO"}])
def test_filtros_nao_misturam_partidas_de_outro_contexto(app, versao, fabrica_captura, filtros):
    _oficial(fabrica_captura)
    resposta = _get(app, **filtros)
    assert resposta.status_code == 200 and resposta.get_json()["partidas"] == []


@pytest.mark.parametrize("parametros,campo", [
    ({"sentido": "ida", "data": "2026-11-19"}, "linha"),
    ({"linha": "3762-10", "data": "2026-11-19"}, "sentido"),
    ({"linha": "3762-10", "sentido": "ida"}, "data"),
    ({**FILTROS, "linha": " "}, "linha"),
    ({**FILTROS, "linha": "x" * 65}, "linha"),
    ({**FILTROS, "linha": "3762-10\n"}, "linha"),
    ({**FILTROS, "sentido": "2"}, "sentido"),
    ({**FILTROS, "data": "2026-02-30"}, "data"),
    ({**FILTROS, "data": "20261119"}, "data"),
    ({**FILTROS, "data": "2026-W47-4"}, "data"),
    ({**FILTROS, "data": "2026-11-19T12:00:00Z"}, "data"),
    ({**FILTROS, "tipo_dia": ""}, "tipo_dia"),
    ({**FILTROS, "tipo_dia": "AUTOMATICO"}, "tipo_dia"),
    ({**FILTROS, "versao_gtfs_id": "invalida"}, "versao_gtfs_id"),
    ({**FILTROS, "versao_gtfs_id": "a" * 32}, "versao_gtfs_id"),
    ({**FILTROS, "outro": "campo"}, "outro"),
    ({**FILTROS, "linha": ["3762-10", "3762-21"]}, "linha"),
    ({**FILTROS, "data": ["2026-11-19", "2026-11-20"]}, "data"),
])
def test_filtros_invalidos_retornam_400_antes_de_consultar_banco(app, monkeypatch, parametros, campo):
    consulta = Mock(side_effect=AssertionError("filtro inválido não consulta banco"))
    monkeypatch.setattr(routes, "consultar_partidas", consulta)
    resposta = app.test_client().get("/partidas", query_string=parametros)
    assert resposta.status_code == 400
    assert resposta.get_json()["erro"]["codigo"] == "FILTRO_INVALIDO"
    assert resposta.get_json()["erro"]["campo"] == campo
    consulta.assert_not_called()


def test_contextos_ausentes_retornam_404_sem_lista_de_sucesso(app, versao):
    for filtros in ({"linha": "0000-00"}, {"versao_gtfs_id": str(uuid4())}, {"data": "2027-01-01"}):
        resposta = _get(app, **filtros)
        assert resposta.status_code == 404 and "erro" in resposta.get_json()
        assert "partidas" not in resposta.get_json()


def test_sem_gtfs_persistido_retorna_erro_de_contexto(app):
    resposta = _get(app)
    assert resposta.status_code == 404
    assert resposta.get_json()["erro"]["codigo"] == "VERSAO_GTFS_NAO_ENCONTRADA"


def test_calendario_ambiguo_exige_tipo_dia_sem_deduzir_pela_semana(app, versao, fabrica_captura):
    _oficial(fabrica_captura)
    _gtfs(versao, tipo_dia="FERIADO")
    resposta = _get(app)
    assert resposta.status_code == 409 and resposta.get_json()["erro"]["codigo"] == "GRADE_AMBIGUA"
    assert _get(app, tipo_dia="FERIADO").get_json()["procedencia"]["fonte"] == "GTFS"
    assert _get(app, tipo_dia="UTIL").get_json()["procedencia"]["fonte"] == "OFICIAL"


def test_vigencia_ambigua_exige_versao_explicita(app, versao):
    outra = VersaoGTFS(hash="b" * 64, capturada_em=AGORA, vigencia_inicio=DATA,
                       arquivo=ArquivoArmazenado(chave="gtfs/b.zip", nome_original="b.zip",
                                                  mime_type="application/zip", tamanho_bytes=0,
                                                  sha256="b" * 64, criado_em=AGORA))
    db.session.add(outra)
    db.session.commit()
    resposta = _get(app)
    assert resposta.status_code == 409
    assert resposta.get_json()["erro"]["codigo"] == "VERSAO_GTFS_AMBIGUA"
    assert _get(app, versao_gtfs_id=versao.id).status_code == 200


def test_route_id_preserva_maiusculas_e_espacos_do_codigo_persistido(app, versao):
    codigo = " rota-A "
    db.session.add(Linha(versao_gtfs_id=versao.id, codigo=codigo, nome="GTFS sintético"))
    db.session.commit()
    _gtfs(versao, codigo=codigo)
    resposta = _get(app, linha=codigo)
    assert resposta.status_code == 200 and resposta.get_json()["linha"] == codigo
    assert _get(app, linha=codigo.upper()).status_code == 404


def test_falha_de_banco_retorna_503_sem_senha_sql_ou_lista_vazia(app, versao, monkeypatch, caplog):
    erro = OperationalError("SELECT SQL_PRIVADO", {}, Exception("SENHA_PRIVADA"))
    monkeypatch.setattr(repositorio, "consultar_por_dia", Mock(side_effect=erro))
    resposta = _get(app)
    assert resposta.status_code == 503
    dados = resposta.get_json()
    assert dados["erro"]["codigo"] == "CONSULTA_INDISPONIVEL" and "partidas" not in dados
    assert "SENHA_PRIVADA" not in resposta.text + caplog.text
    assert "SQL_PRIVADO" not in resposta.text + caplog.text


@pytest.mark.parametrize("fonte", ["OFICIAL", "GTFS"])
def test_get_so_le_banco_sem_buscar_gerar_gravar_ou_criar_arquivos(app, versao, fabrica_captura, monkeypatch, fonte):
    if fonte == "OFICIAL":
        _oficial(fabrica_captura)
    else:
        _gtfs(versao)
    falhar = Mock(side_effect=AssertionError("GET deve apenas consultar"))
    for nome in ("importar_oficial", "gerar_programacao_gtfs", "gerar_partidas", "_armazenamento"):
        monkeypatch.setattr(service, nome, falhar)
    monkeypatch.setattr(requests.Session, "request", falhar)
    monkeypatch.setattr(db.session, "commit", falhar)
    comandos = []
    def registrar(conexao, cursor, comando, parametros, contexto, varios):
        comandos.append(comando.lstrip().split()[0].upper())
    event.listen(db.engine, "before_cursor_execute", registrar)
    try:
        assert _get(app).status_code == 200
        assert set(comandos) == {"SELECT"}
        assert ImportacaoProgramacao.query.count() == 1
        assert PartidaProgramada.query.count() == (3 if fonte == "OFICIAL" else 2)
    finally:
        event.remove(db.engine, "before_cursor_execute", registrar)
    falhar.assert_not_called()


def test_versao_mais_recente_da_mesma_fonte_e_cors_para_frontend(app, versao, fabrica_captura):
    _oficial(fabrica_captura, ("05:00",))
    nova = service.importar_oficial("3762-10", "ida", DATA, "UTIL",
                                   captura=fabrica_captura(("05:10",)), agora=AGORA + timedelta(minutes=1))
    resposta = app.test_client().get("/partidas", query_string=FILTROS,
                                    headers={"Origin": "http://localhost:5173"})
    assert resposta.headers["Access-Control-Allow-Origin"] == "http://localhost:5173"
    assert resposta.get_json()["partidas"] == nova["partidas"]
    assert resposta.get_json()["procedencia"]["versao_fonte"] == nova["procedencia"]["versao_fonte"]
