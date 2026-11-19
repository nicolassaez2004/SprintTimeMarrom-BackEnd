"""MySQL local isolado; nunca usa a base compartilhada por padrão."""

import os
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from pathlib import Path
import pytest
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from flask_migrate import upgrade
from app import create_app, db
from app.models import ExecucaoColeta, RegistroBrutoGPS, LacunaColeta, NotificacaoColeta
from app.coleta.bus_collector import LockColetor
from app.coleta.resiliencia import CicloResiliente
from app.integrations.sptrans import SPTransConexaoError
from app.repositories.coleta import ColetaRepository, PossePerdida
from app.monitoramento.journal import JournalColeta
from app.monitoramento.metricas import medir_gravacao
from test_us29 import Clock, resposta


def test_mysql_migracao_lock_reinicio_outbox_e_metrica(tmp_path, monkeypatch):
    valor = os.getenv("US29_MYSQL_TEST_URL")
    if not valor:
        pytest.skip("US29_MYSQL_TEST_URL em base descartável")
    uri = make_url(valor)
    assert uri.host == "127.0.0.1" and uri.database.startswith("us29_verificacao_")
    app = create_app(
        {"TESTING": True, "SQLALCHEMY_DATABASE_URI": uri, "MYSQL_SSL_CA": None}
    )
    with app.app_context():
        lock = LockColetor(db.engine)
        outro = LockColetor(db.engine)
        try:
            assert not inspect(db.engine).get_table_names()
            upgrade(directory=str(Path(__file__).resolve().parents[2] / "migrations"))
            assert lock.adquirir() and not outro.adquirir()
            clock = Clock()
            journal = JournalColeta(tmp_path, agora=clock.agora)
            repo = ColetaRepository()
            repo.assumir()
            client = SimpleNamespace(
                buscar_todas_posicoes=Mock(
                    side_effect=[SPTransConexaoError("timeout"), resposta(clock)]
                )
            )
            service = CicloResiliente(
                journal,
                repo,
                lambda: client,
                ativo=lock.ativo,
                monotonic=clock.monotonic,
                dormir=clock.dormir,
            )
            assert service.coletar_ciclo(clock.inicio) == 1
            assert ExecucaoColeta.query.one().tentativas == 2
            assert LacunaColeta.query.one().fim is not None
            # Resposta HTTP sem horário: commit do MySQL confirmado, ACK perdido.
            client.buscar_todas_posicoes = Mock(
                return_value=resposta(clock, sem_horario=True)
            )
            original = repo.concluir

            def ack_perdido(*args):
                original(*args)
                raise OperationalError("sql", {}, Exception("perda de ACK"))

            monkeypatch.setattr(repo, "concluir", ack_perdido)
            with pytest.raises(OperationalError):
                service.coletar_ciclo()
            assert RegistroBrutoGPS.query.count() == 2
            clock.dormir(600)
            journal.verificar_alerta()
            assert len(list((tmp_path / "alertas").glob("*.json"))) == 1
            # Novo processo: checkpoint recarregado, sem refazer GET /Posicao.
            j2 = JournalColeta(tmp_path, agora=clock.agora)
            repo2 = ColetaRepository()
            repo2.assumir()
            repo2.restaurar(j2, 30, clock.agora())
            sem_get = Mock(side_effect=AssertionError("nao consultar fonte"))
            novo = CicloResiliente(
                j2,
                repo2,
                sem_get,
                ativo=lock.ativo,
                monotonic=clock.monotonic,
                dormir=clock.dormir,
            )
            assert novo.coletar_ciclo() == 1
            assert sem_get.call_count == 0 and RegistroBrutoGPS.query.count() == 2
            assert NotificacaoColeta.query.count() == 2
            assert (
                ExecucaoColeta.query.order_by(ExecucaoColeta.id.desc())
                .first()
                .duracao_gravacao_ms
                == 600000
            )
            rel = medir_gravacao(clock.inicio, clock.agora() + timedelta(seconds=1))
            assert not rel["meta_atingida"] and rel["sem_ciclo_registrado"] > 0
            # Mata somente a conexão de lock criada por este teste.
            id_conexao = lock.conexao.execute(
                text("SELECT CONNECTION_ID()")
            ).scalar_one()
            with db.engine.connect() as admin:
                admin.execute(text(f"KILL CONNECTION {int(id_conexao)}"))
            assert not lock.ativo()
            substituto = LockColetor(db.engine)
            assert substituto.adquirir()
            try:
                atual = ColetaRepository()
                atual.assumir()
                with pytest.raises(PossePerdida):
                    repo.reservar()
                db.session.rollback()
            finally:
                substituto.liberar()
        finally:
            lock.liberar()
            outro.liberar()
            db.session.remove()
            db.engine.dispose()
