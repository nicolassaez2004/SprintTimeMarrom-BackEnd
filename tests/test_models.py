from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable

from app import create_app, db
from app.coleta import bus_collector
from app.models import ExecucaoColeta, OnibusSelecionado, RegistroBrutoGPS, Veiculo

INICIO = datetime(2026, 11, 17, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
def app():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://"})
    with app.app_context():
        db.create_all()
        db.session.add(Veiculo(prefixo="5810"))
        db.session.add(OnibusSelecionado(prefixo="5810", codigo_linha=123))
        db.session.commit()
        yield app
        db.session.remove()


def _execucao(estado, minutos, **campos):
    execucao = ExecucaoColeta(
        iniciada_em=INICIO + timedelta(minutes=minutos),
        finalizada_em=INICIO + timedelta(minutes=minutos, seconds=2),
        estado=estado,
        **campos,
    )
    db.session.add(execucao)
    db.session.flush()
    return execucao


def test_exemplos_completo_incompleto_e_falha_sao_representados(app):
    completa = _execucao("OK", 0, novos_persistidos=1)
    db.session.add(RegistroBrutoGPS(
        execucao_id=completa.id, veiculo_prefixo="5810", prefixo_origem="5810",
        linha_origem="123",
        latitude=-23.550520, longitude=-46.633308,
        horario_gps=INICIO, coletado_em=INICIO,
    ))
    incompleta = _execucao("OK", 0.5, novos_persistidos=1)
    # Campos ausentes na API ficam NULL.
    db.session.add(RegistroBrutoGPS(
        execucao_id=incompleta.id, prefixo_origem="5810", coletado_em=INICIO,
        payload={"p": "5810"},
    ))
    _execucao("FALHA", 1, erro="RuntimeError")
    db.session.commit()

    registros = RegistroBrutoGPS.query.order_by(RegistroBrutoGPS.id).all()
    assert registros[0].completo
    assert registros[0].latitude == pytest.approx(-23.550520, abs=1e-6)
    assert not registros[1].completo
    assert (registros[1].latitude, registros[1].longitude, registros[1].horario_gps) == (
        None, None, None
    )
    assert registros[1].payload == {"p": "5810"}
    assert registros[1].veiculo is None  # prefixo de origem sem Veiculo resolvido
    falha = ExecucaoColeta.query.filter_by(estado="FALHA").one()
    assert falha.erro == "RuntimeError"
    assert falha.registros_gps == []


def test_equipe_conta_ciclos_esperados_e_gravados(app):
    # Janela de 2 minutos a 30 s = 4 ciclos esperados; um não foi gravado.
    for minutos in (0, 0.5, 1.5):
        _execucao("OK", minutos, novos_persistidos=1)
    db.session.commit()

    janela = timedelta(minutes=2)
    esperados = int(janela.total_seconds() // 30)
    gravados = ExecucaoColeta.query.filter(
        ExecucaoColeta.iniciada_em >= INICIO,
        ExecucaoColeta.iniciada_em < INICIO + janela,
    ).count()
    assert (esperados, gravados) == (4, 3)


def test_horarios_sao_gravados_e_lidos_em_utc(app):
    horario_sp = datetime(2026, 11, 17, 12, 0, 0, 123456,
                          tzinfo=timezone(timedelta(hours=-3)))
    execucao = _execucao("OK", 0)
    execucao.iniciada_em = horario_sp
    db.session.commit()
    db.session.expire_all()

    lido = db.session.get(ExecucaoColeta, execucao.id).iniciada_em
    assert lido.tzinfo == timezone.utc
    assert lido == datetime(2026, 11, 17, 15, 0, 0, 123456, tzinfo=timezone.utc)


def test_horario_sem_fuso_e_rejeitado(app):
    db.session.add(ExecucaoColeta(iniciada_em=datetime(2026, 11, 17, 15, 0)))
    with pytest.raises(Exception, match="fuso"):
        db.session.commit()
    db.session.rollback()


def test_ddl_mysql_usa_double_datetime6_e_nulls(app):
    ddl = {
        tabela.name: str(CreateTable(tabela).compile(dialect=mysql.dialect()))
        for tabela in db.metadata.sorted_tables
    }
    registros = ddl["registros_gps"]
    assert "latitude DOUBLE," in registros
    assert "longitude DOUBLE," in registros
    assert "horario_gps DATETIME(6)," in registros
    assert "coletado_em DATETIME(6) NOT NULL" in registros
    assert "payload JSON" in registros
    assert "ck_execucoes_coleta_estado" in ddl["execucoes_coleta"]


def test_mesma_posicao_em_ciclos_seguidos_e_gravada_uma_vez(app):
    resposta = {"vs": [
        {"p": "5810", "py": -23.5, "px": -46.6, "ta": "2026-11-17T15:00:00Z"},
    ]}
    cliente = SimpleNamespace(buscar_posicoes=Mock(return_value=resposta))
    with patch.object(bus_collector, "SPTransClient", return_value=cliente):
        coletor = bus_collector.BusCollector()
        assert coletor.coletar_ciclo() == 1
        # A API devolve a mesma posição (mesmo `ta`) no ciclo seguinte.
        assert coletor.coletar_ciclo() == 0

    assert RegistroBrutoGPS.query.count() == 1
    primeira, segunda = ExecucaoColeta.query.order_by(ExecucaoColeta.id).all()
    assert RegistroBrutoGPS.query.one().execucao_id == primeira.id
    assert (segunda.recebidos, segunda.novos_persistidos) == (1, 0)


def test_sem_horario_em_ciclos_diferentes_todos_sao_preservados(app):
    resposta = {"vs": [{"p": "5810", "py": -23.5, "px": -46.6}]}
    cliente = SimpleNamespace(buscar_posicoes=Mock(return_value=resposta))
    with patch.object(bus_collector, "SPTransClient", return_value=cliente):
        coletor = bus_collector.BusCollector()
        assert coletor.coletar_ciclo() == 1
        assert coletor.coletar_ciclo() == 1

    registros = RegistroBrutoGPS.query.order_by(RegistroBrutoGPS.id).all()
    assert len(registros) == 2
    assert registros[0].execucao_id != registros[1].execucao_id


def test_desmarcar_onibus_preserva_historico_bruto(app):
    resposta = {"vs": [{"p": "5810", "py": -23.5, "px": -46.6, "ta": "2026-11-17T15:00:00Z"}]}
    cliente = SimpleNamespace(buscar_posicoes=Mock(return_value=resposta))
    with patch.object(bus_collector, "SPTransClient", return_value=cliente):
        bus_collector.BusCollector().coletar_ciclo()

    db.session.delete(db.session.get(OnibusSelecionado, "5810"))
    db.session.commit()
    assert RegistroBrutoGPS.query.one().veiculo.prefixo == "5810"
