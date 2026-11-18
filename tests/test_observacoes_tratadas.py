from copy import deepcopy
from datetime import datetime, timezone

import pytest
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError

from app import create_app, db
from app.models import ExecucaoColeta, RegistroBrutoGPS, ObservacaoTratada


@pytest.fixture
def contexto():
    app = create_app({'TESTING': True, 'SQLALCHEMY_DATABASE_URI': 'sqlite://'})
    with app.app_context():
        @event.listens_for(db.engine, 'connect')
        def ativar_fks(connection, _):
            connection.execute('PRAGMA foreign_keys=ON')
        db.create_all()
        ciclo = ExecucaoColeta(estado='OK', novos_persistidos=1)
        db.session.add(ciclo)
        db.session.flush()
        bruto = RegistroBrutoGPS(
            execucao_id=ciclo.id, prefixo_origem='5810',
            latitude=None, longitude=None,
            coletado_em=datetime(2026, 11, 18, tzinfo=timezone.utc),
            payload={'p': '5810', 'py': 'invalida', 'extra': {'valor': 'original'}},
        )
        db.session.add(bruto)
        db.session.commit()
        yield bruto
        db.session.remove()
        db.engine.dispose()


def test_tratamento_preserva_payload_e_campos_originais(contexto):
    bruto = contexto
    payload, instante = deepcopy(bruto.payload), bruto.coletado_em
    assert bruto.observacao_tratada is None
    obs = ObservacaoTratada(bruto_id=bruto.id, valida=False,
                            motivo_invalidez='coordenada inválida',
                            versao_correspondencia='validacao-v1')
    db.session.add(obs)
    db.session.commit()
    db.session.expire_all()
    assert bruto.observacao_tratada.id == obs.id
    assert obs.bruto.id == bruto.id and obs.versao_gtfs is None
    assert bruto.payload == payload
    assert bruto.latitude is None and bruto.coletado_em == instante
    obs.valida = True
    obs.motivo_invalidez = None
    db.session.commit()
    db.session.expire_all()
    assert bruto.payload == payload and bruto.latitude is None


def test_nao_permite_dois_tratamentos_para_mesmo_bruto(contexto):
    for _ in range(2):
        db.session.add(ObservacaoTratada(bruto_id=contexto.id, valida=True,
                                        versao_correspondencia='v1'))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()
    assert ObservacaoTratada.query.count() == 0
    assert RegistroBrutoGPS.query.count() == 1


@pytest.mark.parametrize('campo,valor', [
    ('bruto_id', 999999), ('bruto_id', None), ('versao_gtfs_id', 'inexistente'),
    ('versao_correspondencia', ''), ('versao_correspondencia', '   '),
    ('valida', None),
])
def test_rejeita_tratado_inconsistente(contexto, campo, valor):
    dados = dict(bruto_id=contexto.id, valida=True, versao_correspondencia='v1')
    dados[campo] = valor
    db.session.add(ObservacaoTratada(**dados))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()
    assert RegistroBrutoGPS.query.count() == 1


def test_nao_apaga_origem_referenciada(contexto):
    db.session.add(ObservacaoTratada(bruto_id=contexto.id, valida=True,
                                    versao_correspondencia='v1'))
    db.session.commit()
    db.session.delete(contexto)
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()
    assert RegistroBrutoGPS.query.count() == ObservacaoTratada.query.count() == 1
