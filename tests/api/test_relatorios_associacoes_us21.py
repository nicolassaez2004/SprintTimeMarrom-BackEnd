from copy import deepcopy
import json
from pathlib import Path
import runpy

import pytest

from app import create_app, db
from app.analises.associacoes import digest
from app.analises.coeficientes import calcular_associacoes
from app.analises.plano import hash_conteudo_plano
from app.models import LimiteConsultaIP, RelatorioAssociacaoMensal
from app.relatorios.associacoes import registrar_invalido, registrar_publicado

RAIZ = Path(__file__).resolve().parents[2]


@pytest.fixture
def client():
    app = create_app({'TESTING': True, 'SQLALCHEMY_DATABASE_URI': 'sqlite:///:memory:'})
    with app.app_context():
        db.metadata.create_all(bind=db.engine, tables=[LimiteConsultaIP.__table__, RelatorioAssociacaoMensal.__table__])
    return app.test_client()


@pytest.fixture
def ensaio():
    # Exclusivamente para teste: não representa aprovação institucional ou dados reais.
    helper = runpy.run_path(str(RAIZ/'tests/analises/test_coeficientes_us21.py'))
    linhas, agrup = helper['base_controlada']()
    plano = json.loads((RAIZ/'docs/analises/US21-plano-v1.json').read_text())
    template = json.loads((RAIZ/'docs/validacoes/US21-T01-amostra.json').read_text())
    plano['agrupamentos'].update(agrup)
    bases = []
    for mes in sorted({r['mes'] for r in linhas}):
        b = {'mes': mes, 'versao_base': template['versao_base'], 'politica': {'teste': True},
             'selecao': deepcopy(template['selecao']),
             'resumo': {'periodo_disponivel': {'inferior_ao_mes': False}},
             'linhas': [r for r in linhas if r['mes'] == mes]}
        b['selecao']['versao_gtfs_id'] = 'rede'
        b['hash_base'] = digest(b)
        bases.append(b)
    plano['qualidade']['hash_politica_base_aprovada'] = digest(bases[0]['politica'])
    plano['qualidade']['hashes_bases_aprovadas'] = [b['hash_base'] for b in bases]
    plano['situacao'] = 'APROVADO'
    h = hash_conteudo_plano(plano)
    for a in plano['aprovacoes'].values():
        a.update(nome='FICTICIO', instituicao='FICTICIA', data='2026-11-27', referencia='urn:teste', hash_conteudo_plano=h)
    resultado = calcular_associacoes(plano, bases)
    assert resultado['situacao'] == 'CALCULADO'
    revisao = {'responsavel': 'REVISOR_FICTICIO', 'data': '2026-11-27', 'referencia': 'urn:teste',
               'hash_resultado': digest(resultado), 'diagnosticos_aprovados': True, 'publicacao_aprovada': True}
    return resultado, bases, revisao


def test_publicado_e_mes_ausente(client, ensaio):
    resultado, bases, revisao = ensaio
    with client.application.app_context():
        registrar_publicado('2025-01', resultado, bases, revisao)
        db.session.commit()
    resposta = client.get('/relatorios/associacoes?mes=2025-01')
    body = resposta.get_json()
    assert resposta.status_code == 200
    assert body['situacao'] == 'PUBLICADO' and len(body['fontes']) == 8
    assert body['resultado']['modelos']['secundario']['coeficientes']['chuva_10mm']['ic95'] == pytest.approx([1.24410545, 2.75589455])
    assert body['resultado']['modelos']['principal']['observacoes'] == 160
    assert body['resultado']['versoes'] and body['resultado']['parametros']
    assert body['resultado']['modelos']['principal']['limitacoes']
    ausente = client.get('/api/relatorios/associacoes?mes=2025-02')
    assert ausente.status_code == 404 and ausente.get_json()['situacao'] == 'NAO_ENCONTRADO'
    assert 'resultado' not in ausente.get_json()


def test_invalido_e_dados_corrompidos_nao_publicam_coeficientes(client, ensaio):
    resultado, bases, revisao = ensaio
    with client.application.app_context():
        registrar_invalido('2025-02', 'MATRIZ_SEM_POSTO_COMPLETO')
        registrar_publicado('2025-01', resultado, bases, revisao)
        db.session.commit()
        registro = db.session.get(RelatorioAssociacaoMensal, '2025-01')
        alterado = deepcopy(registro.dados)
        alterado['resultado']['modelos']['principal']['observacoes'] = 999
        registro.dados = alterado
        db.session.commit()
    for mes in ('2025-01', '2025-02'):
        r = client.get('/relatorios/associacoes?mes='+mes)
        assert r.status_code == 409 and r.get_json()['situacao'] == 'INVALIDO'
        assert 'resultado' not in r.get_json()


def test_registro_exige_revisao_bases_e_analise_valida(client, ensaio):
    resultado, bases, revisao = ensaio
    with client.application.app_context():
        for revisao_errada in ({}, {**revisao, 'hash_resultado': 'outro'}, {**revisao, 'diagnosticos_aprovados': False}):
            with pytest.raises(ValueError): registrar_publicado('2025-01', resultado, bases, revisao_errada)
        with pytest.raises(ValueError): registrar_publicado('2025-02', resultado, bases, revisao)
        with pytest.raises(ValueError): registrar_publicado('2025-01', {**resultado, 'situacao': 'INCOMPLETO'}, bases, revisao)
        adulteradas = deepcopy(bases); adulteradas[0]['linhas'][0]['desempenho_s'] = 999
        with pytest.raises(ValueError): registrar_publicado('2025-01', resultado, adulteradas, revisao)
        sinteticas = deepcopy(bases); sinteticas[0]['origem'] = 'SINTETICA_TESTE'
        with pytest.raises(ValueError): registrar_publicado('2025-01', resultado, sinteticas, revisao)
        assert db.session.get(RelatorioAssociacaoMensal, '2025-01') is None


@pytest.mark.parametrize('query', ['', '?mes=2025-13', '?mes=9999-12', '?mes=2025-1', '?mes=2025-01&mes=2025-02', '?mes=2025-01&x=1'])
def test_parametros_invalidos(client, query):
    assert client.get('/relatorios/associacoes'+query).status_code == 400


def test_cors_frontend(client):
    r = client.get('/relatorios/associacoes?mes=2025-01', headers={'Origin': 'http://localhost:5173'})
    assert r.headers['Access-Control-Allow-Origin'] == 'http://localhost:5173'


def test_reprocessamento_pode_substituir_invalido_mas_nao_publicado(client, ensaio):
    resultado, bases, revisao = ensaio
    with client.application.app_context():
        registrar_invalido('2025-01', 'AMOSTRA_INSUFICIENTE')
        db.session.commit()
        registrar_publicado('2025-01', resultado, bases, revisao)
        db.session.commit()
        with pytest.raises(ValueError):
            registrar_publicado('2025-01', resultado, bases, revisao)
    assert client.get('/relatorios/associacoes?mes=2025-01').status_code == 200


def test_migracao_cria_tabela_no_sqlite():
    import importlib.util
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, inspect

    spec = importlib.util.spec_from_file_location('us21_migration', RAIZ/'migrations/versions/us21_0027_relatorios_associacao.py')
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    engine = create_engine('sqlite:///:memory:')
    with engine.begin() as conexao:
        modulo.op = Operations(MigrationContext.configure(conexao))
        modulo.upgrade()
        inspetor = inspect(conexao)
        assert 'relatorios_associacao_mensal' in inspetor.get_table_names()
        assert {c['name'] for c in inspetor.get_columns('relatorios_associacao_mensal')} == {'mes', 'situacao', 'dados', 'hash_dados', 'registrado_em'}
