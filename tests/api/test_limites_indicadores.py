import importlib.util
import io
import multiprocessing
from collections import Counter
from pathlib import Path
from unittest.mock import Mock

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine
from sqlalchemy.dialects import mysql
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.middleware.proxy_fix import ProxyFix

from app import create_app, db
from app.core.limites import comando_consumo
from app.indicadores import routes
from app.models import IndicadorOperacional, LimiteConsultaIP


@pytest.fixture
def contexto(tmp_path):
    relogio = [120.5]
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": f"sqlite:///{(tmp_path / 'limites.sqlite').as_posix()}",
        "MYSQL_SSL_CA": None, "INDICADORES_TRUSTED_PROXY_CIDRS": "", "LIMITES_RELOGIO": lambda: relogio[0]})
    with app.app_context():
        db.metadata.create_all(db.engine, tables=[IndicadorOperacional.__table__, LimiteConsultaIP.__table__])
    yield app, relogio
    with app.app_context():
        db.session.remove()
        db.engine.dispose()


def pedir(app, path="/indicadores", ip="203.0.113.7", **kwargs):
    return app.test_client().get(path, environ_overrides={"REMOTE_ADDR": ip}, **kwargs)


def esgotar(app, **kwargs):
    for n in range(60):
        r = pedir(app, "/series" if n % 2 else "/indicadores", **kwargs)
        assert r.status_code == 200
        assert int(r.headers["X-RateLimit-Remaining"]) == 59 - n


def test_sexagesima_primeira_compartilha_cota_das_duas_rotas_e_informa_retry(contexto):
    app, _ = contexto
    esgotar(app)
    r = pedir(app)
    assert r.status_code == 429 and r.headers["Retry-After"] == "60"
    assert r.headers["X-RateLimit-Limit"] == "60" and r.headers["X-RateLimit-Remaining"] == "0"
    assert r.headers["X-RateLimit-Reset"] == "180"
    assert r.get_json()["erro"]["codigo"] == "LIMITE_EXCEDIDO"
    assert r.get_json()["erro"]["tentar_em_segundos"] == 60
    assert r.headers["Cache-Control"] == "no-store"
    assert pedir(app, "/series").status_code == 429


def test_ip_diferente_e_nova_janela_funcionam_e_cota_sobrevive_a_novo_app(contexto):
    app, relogio = contexto
    esgotar(app)
    assert pedir(app, ip="203.0.113.8").status_code == 200
    outra = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": app.config["SQLALCHEMY_DATABASE_URI"],
        "MYSQL_SSL_CA": None, "INDICADORES_TRUSTED_PROXY_CIDRS": "", "LIMITES_RELOGIO": lambda: relogio[0]})
    assert pedir(outra).status_code == 429  # Outra instância/processo usa a mesma tabela.
    with outra.app_context():
        db.session.remove()
        db.engine.dispose()
    relogio[0] = 179.8
    assert pedir(app).headers["Retry-After"] == "1"
    relogio[0] = 180
    nova = pedir(app)
    assert nova.status_code == 200 and nova.headers["X-RateLimit-Remaining"] == "59"
    assert nova.headers["Cache-Control"] == "no-store"


def test_cabecalhos_arbitrarios_nao_trocam_identidade_da_conexao(contexto):
    app, _ = contexto
    for n in range(61):
        r = pedir(app, headers={"X-Forwarded-For": f"198.51.100.{n}", "X-Real-IP": f"192.0.2.{n}",
                              "Forwarded": f"for=192.0.2.{n}"})
        assert r.status_code == (200 if n < 60 else 429)
    with app.app_context():
        assert LimiteConsultaIP.query.count() == 1
        assert LimiteConsultaIP.query.one().chave_ip != "203.0.113.7"


def test_proxy_confiavel_percorre_cadeia_pela_direita_sem_aceitar_prefixo_forjado(contexto):
    app, _ = contexto
    # Alteração da confiança em teste equivale à configuração validada no startup.
    from app.core.limites import configurar_limites
    app.config["INDICADORES_TRUSTED_PROXY_CIDRS"] = "10.0.0.1/32,10.0.0.2/32"
    configurar_limites(app)
    for n in range(61):
        r = pedir(app, ip="10.0.0.1", headers={"X-Forwarded-For": f"192.0.2.{n},203.0.113.7,10.0.0.2"})
        assert r.status_code == (200 if n < 60 else 429)
    assert pedir(app, ip="10.0.0.1", headers={"X-Forwarded-For": "203.0.113.8,10.0.0.2"}).status_code == 200
    # Conexão de fora da lista ignora inclusive um cabeçalho sintaticamente inválido.
    assert pedir(app, ip="203.0.113.9", headers={"X-Forwarded-For": "não é IP"}).status_code == 200
    assert pedir(app, ip="10.0.0.1", headers={"X-Forwarded-For": "não é IP"}).status_code == 400


def test_proxyfix_preexistente_nao_torna_header_forjado_confiavel(contexto):
    app, _ = contexto
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)
    for n in range(61):
        r = pedir(app, headers={"X-Forwarded-For": f"192.0.2.{n}"})
        assert r.status_code == (200 if n < 60 else 429)


def test_ipv4_mapeado_ipv6_nao_ganha_outra_cota(contexto):
    app, _ = contexto
    esgotar(app)
    assert pedir(app, ip="::ffff:203.0.113.7").status_code == 429
    assert pedir(app, ip="2001:db8::1").status_code == 200


def test_head_e_filtros_invalidos_contam_options_e_health_nao(contexto):
    app, _ = contexto
    cliente = app.test_client()
    for _ in range(65):
        assert cliente.options("/indicadores").status_code == 200
        assert cliente.get("/health").status_code == 200
    for _ in range(59):
        assert cliente.head("/indicadores").status_code == 200
    assert cliente.get("/series?page_size=101").status_code == 400
    assert cliente.get("/indicadores").status_code == 429


def test_falha_da_consulta_nao_devolve_cota_e_cors_expoe_retry(contexto, monkeypatch):
    app, _ = contexto
    with monkeypatch.context() as patch:
        patch.setattr(routes, "consultar_indicadores", Mock(side_effect=SQLAlchemyError("segredo SQL")))
        assert pedir(app).status_code == 503
    assert pedir(app).headers["X-RateLimit-Remaining"] == "58"
    r = pedir(app, headers={"Origin": "http://localhost:5173"})
    assert "Retry-After" in r.headers["Access-Control-Expose-Headers"]


def test_sem_armazenamento_compartilhado_retorna_503_sem_liberar_cota_local(contexto):
    app, _ = contexto
    with app.app_context():
        LimiteConsultaIP.__table__.drop(db.engine)  # Somente o SQLite descartável da fixture.
    r = pedir(app)
    assert r.status_code == 503 and r.headers["Retry-After"] == "5"
    assert r.get_json()["erro"]["codigo"] == "LIMITE_INDISPONIVEL"
    assert "limites_consultas_ip" not in r.text and "sqlite" not in r.text


def test_sqlite_em_memoria_nao_e_aceito_como_contador_compartilhado_fora_dos_testes():
    app = create_app({"TESTING": False, "SQLALCHEMY_DATABASE_URI": "sqlite://", "MYSQL_SSL_CA": None})
    assert pedir(app).status_code == 503
    assert app.test_client().get("/health").status_code == 200
    with app.app_context():
        db.engine.dispose()


def test_relogio_retrocedendo_nao_reinicia_orcamento(contexto):
    app, relogio = contexto
    esgotar(app)
    relogio[0] = 119
    assert pedir(app).status_code == 429


@pytest.mark.parametrize("redes", ["qualquer", "0.0.0.0/0", "::/0"])
def test_configuracoes_de_proxy_irrestritas_ou_invalidas_sao_rejeitadas(redes):
    with pytest.raises(ValueError):
        create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://",
                    "MYSQL_SSL_CA": None, "INDICADORES_TRUSTED_PROXY_CIDRS": redes})


def _consumir_em_processo(uri, barreira, fila):
    try:
        app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": uri, "MYSQL_SSL_CA": None,
            "SQLALCHEMY_ENGINE_OPTIONS": {"connect_args": {"timeout": 15}},
            "INDICADORES_TRUSTED_PROXY_CIDRS": "", "LIMITES_RELOGIO": lambda: 120})
        barreira.wait(timeout=30)
        codigos = [pedir(app).status_code for _ in range(40)]
        fila.put(codigos)
        with app.app_context():
            db.session.remove()
            db.engine.dispose()
    except BaseException as erro:
        fila.put(type(erro).__name__)
        raise


def test_dois_processos_concorrentes_compartilham_exatamente_sessenta_permissoes(contexto):
    app, _ = contexto
    ctx = multiprocessing.get_context("spawn")
    barreira, fila = ctx.Barrier(2), ctx.Queue()
    processos = [ctx.Process(target=_consumir_em_processo, args=(app.config["SQLALCHEMY_DATABASE_URI"], barreira, fila)) for _ in range(2)]
    try:
        for processo in processos:
            processo.start()
        for processo in processos:
            processo.join(timeout=40)
            assert processo.exitcode == 0
        codigos = fila.get(timeout=5) + fila.get(timeout=5)
        assert Counter(codigos) == {200: 60, 429: 20}
    finally:
        for processo in processos:
            if processo.is_alive():
                processo.terminate()
                processo.join(timeout=5)
        fila.close()


def test_migracao_e_upsert_mysql_preservam_o_modelo_e_ordem_de_atribuicao(contexto):
    arquivo = Path(__file__).resolve().parents[2] / "migrations/versions/us11_0016_limites_consultas_ip.py"
    spec = importlib.util.spec_from_file_location("migracao_limites", arquivo)
    migracao = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migracao)
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as conexao:
            db.metadata.create_all(conexao, tables=[t for t in db.metadata.sorted_tables if t.name != "limites_consultas_ip"])
            contexto_migracao = MigrationContext.configure(conexao)
            with Operations.context(contexto_migracao):
                migracao.upgrade()
            assert compare_metadata(contexto_migracao, db.metadata) == []
        saida = io.StringIO()
        offline = MigrationContext.configure(dialect_name="mysql", opts={"as_sql": True, "output_buffer": saida})
        with Operations.context(offline):
            migracao.upgrade()
        assert "ENGINE=InnoDB" in saida.getvalue()
        sql = str(comando_consumo("mysql", "a" * 64, 2).compile(dialect=mysql.dialect()))
        atualizacoes = sql.split("ON DUPLICATE KEY UPDATE")[1]
        assert atualizacoes.index("tentativas =") < atualizacoes.index("janela =")
        assert "least(" in sql and "greatest(" in sql
    finally:
        engine.dispose()
