"""US30 (revisão): coleta, sondagens, disponibilidade mensal e GET /saude (SQLite)."""
from datetime import datetime, timedelta, timezone

import pytest
import requests
from sqlalchemy.exc import OperationalError

from app import create_app, db
from app.models import ExecucaoColeta, LacunaColeta, VerificacaoSaude
from app.monitoramento.disponibilidade_mensal import _servico, disponibilidade_mensal, limites_mes
from app.monitoramento.sondagem import sondar_e_gravar, verificar
from app.monitoramento.sucesso_coleta import classificar_ciclo, serie_sucesso_coleta, taxa_coleta

UTC = timezone.utc
T0 = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)
M = lambda n: timedelta(minutes=n)  # noqa: E731


@pytest.fixture
def app(tmp_path):
    aplicacao = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://", "MYSQL_SSL_CA": None})
    aplicacao.instance_path = str(tmp_path)
    with aplicacao.app_context():
        db.create_all()
        yield aplicacao
        db.session.remove()
        db.engine.dispose()


def ciclo(n, estado="OK", erro=None, **extra):
    instante = T0 + timedelta(seconds=30 * n)
    return ExecucaoColeta(fonte="OLHO_VIVO", prevista_em=instante, iniciada_em=instante + timedelta(seconds=1),
                          estado=estado, erro=erro, recebidos=extra.pop("recebidos", 10), **extra)


def sonda(componente, instante, estado="OK", detalhe="OK"):
    return VerificacaoSaude(componente=componente, verificada_em=instante, estado=estado, latencia_ms=80,
                            detalhe=detalhe, alvo="GET /saude", status_http=200 if estado == "OK" else 503)


# --- US30-T01A: classificação e fórmula da coleta -------------------------------------

@pytest.mark.parametrize("estado,erro,categoria", [
    ("OK", None, "sucesso"),
    ("OK", "SPTransTimeoutError", "sucesso"),  # recuperou nas retentativas do mesmo ciclo
    ("FALHA", "SPTRANS_HTTP_503", "falha_fonte"),
    ("FALHA", "SPTransRespostaInvalidaError", "falha_fonte"),
    ("FALHA", "SPTransTimeoutError", "indeterminada"),
    ("FALHA", "SPTransConexaoError", "indeterminada"),
    ("FALHA", "SPTRANS_HTTP_429", "indeterminada"),
    ("FALHA", "SPTRANS_HTTP_401", "falha_propria"),
    ("FALHA", "SPTransConfiguracaoError", "falha_propria"),
    ("FALHA", "OperationalError", "falha_propria"),
    ("FALHA", "PROCESSO_INTERROMPIDO", "falha_propria"),
    ("FALHA", None, "indeterminada"),
    ("FALHA", "ValueError", "indeterminada"),
    ("PARCIAL", None, "indeterminada"),
    ("EM_ANDAMENTO", None, "indeterminada"),
])
def test_classificacao_segue_evidencia(estado, erro, categoria):
    assert classificar_ciclo(ExecucaoColeta(estado=estado, erro=erro))[0] == categoria


def test_formula_da_coleta_expoe_componentes(app):
    lacuna = LacunaColeta(id="lac-1", inicio=T0 + M(8), fim=T0 + M(9), causa="SPTRANS_HTTP_503",
                          fonte="OLHO_VIVO")
    db.session.add(lacuna)
    ciclos = [ciclo(n) for n in range(13)] + [ciclo(13, recebidos=0, tentativas=4)]  # 14 OK
    ciclos += [ciclo(14, "FALHA", "SPTRANS_HTTP_503", lacuna_id="lac-1"),
               ciclo(15, "FALHA", "SPTransTimeoutError"), ciclo(16, "FALHA", "OperationalError"),
               ciclo(17, "FALHA", "SPTRANS_HTTP_401")]  # ciclos 18 e 19 não aconteceram
    db.session.add_all(ciclos)
    db.session.commit()
    t = taxa_coleta(T0, T0 + M(10), 30)
    assert (t["ciclos_esperados"], t["ciclos_registrados"], t["ciclos_nao_registrados"]) == (20, 18, 2)
    assert (t["sucessos"], t["falhas_proprias"], t["falhas_fonte_excluidas"],
            t["indeterminados_registrados"], t["indeterminados_total"]) == (14, 2, 1, 1, 3)
    assert t["sucessos_sem_veiculos"] == 1 and t["lacunas_vinculadas"] == ["lac-1"]
    assert (t["numerador"], t["denominador"]) == (14, 19)  # só a falha da fonte sai
    assert t["percentual"] == round(14 / 19 * 100, 4)
    assert t["percentual_maximo"] == round(14 / 16 * 100, 4)


def test_denominador_zero_gera_ausencia_com_motivo(app):
    assert taxa_coleta(T0, T0, 30)["motivo_sem_percentual"] == "periodo_sem_ciclos_esperados"
    db.session.add_all([ciclo(0, "FALHA", "SPTRANS_HTTP_500"), ciclo(1, "FALHA", "SPTRANS_HTTP_502")])
    db.session.commit()
    t = taxa_coleta(T0, T0 + M(1), 30)
    assert t["percentual"] is None
    assert t["motivo_sem_percentual"] == "todos_os_ciclos_esperados_tiveram_falha_da_fonte"


def test_serie_permite_auditar_ids_por_categoria(app):
    db.session.add_all([ciclo(0), ciclo(1, "FALHA", "SPTRANS_HTTP_503"), ciclo(130)])
    db.session.commit()
    serie = serie_sucesso_coleta(T0, T0 + M(120), janela=M(60))
    primeiro = serie["pontos"][0]
    assert len(primeiro["ciclos"]["sucesso"]) == 1 and len(primeiro["ciclos"]["falha_fonte"]) == 1
    assert serie["pontos"][1]["sucessos"] == 1


# --- US30-T01B: sondagens -------------------------------------------------------------

class Resposta:
    def __init__(self, status=200, corpo=b"ok"):
        self.status_code, self.corpo = status, corpo

    def iter_content(self, chunk_size):
        yield self.corpo

    def close(self):
        pass


class Sessao:
    def __init__(self, resultado):
        self.resultado, self.chamadas = resultado, []

    def get(self, url, **opcoes):
        self.chamadas.append((url, opcoes))
        if isinstance(self.resultado, Exception):
            raise self.resultado
        return self.resultado


def test_sonda_registra_resultado_duracao_e_alvo_sem_host():
    relogio = iter([10.0, 14.2])  # resposta lenta, mas dentro do timeout
    m = verificar("API", "https://api.exemplo/saude?mes=2026-11", texto_esperado="servicos",
                  sessao=Sessao(Resposta(corpo=b'{"servicos":{}}')), relogio=lambda: next(relogio),
                  agora=lambda: T0, timeout=5)
    assert (m.componente, m.estado, m.detalhe, m.latencia_ms, m.alvo) == ("API", "OK", "OK", 4200, "GET /saude")
    assert m.verificada_em == T0


@pytest.mark.parametrize("resultado,detalhe", [
    (requests.ConnectionError("recusada"), "ERRO_CONEXAO"),
    (requests.Timeout("lenta"), "TIMEOUT"),
    (Resposta(503), "HTTP_ERRO"),
    (Resposta(200, b"<html>erro</html>"), "CONTEUDO_INESPERADO"),
])
def test_sonda_indisponivel(resultado, detalhe):
    m = verificar("PAINEL", "http://painel.local/", texto_esperado="Observa SP", sessao=Sessao(resultado))
    assert (m.estado, m.detalhe) == ("INDISPONIVEL", detalhe)


def test_sonda_recusa_credencial_na_url():
    with pytest.raises(ValueError):
        verificar("API", "https://usuario:senha@api.exemplo/saude")


def test_falha_de_um_alvo_nao_impede_o_outro(app):
    def verificador(componente, url, **_):
        if componente == "API":
            raise RuntimeError("falha inesperada na sonda")
        return sonda("PAINEL", T0)

    gravadas = sondar_e_gravar({"API": ("http://a/saude", None), "PAINEL": ("http://p/", None)},
                               verificador=verificador)
    assert gravadas["API"] is None and gravadas["PAINEL"] is not None
    assert [v.componente for v in VerificacaoSaude.query.all()] == ["PAINEL"]


def test_script_exige_alvos(monkeypatch):
    from scripts.sondagem import main
    monkeypatch.delenv("SAUDE_URL_API", raising=False)
    monkeypatch.delenv("SAUDE_URL_PAINEL", raising=False)
    with pytest.raises(SystemExit):
        main([])


# --- US30-T02: cálculo mensal ---------------------------------------------------------

def _minutos(n, falhas=0, lacunas=0):
    inicio = T0
    todos = [inicio + M(i) for i in range(n)]
    observados = todos[lacunas:]
    return {m: i >= falhas for i, m in enumerate(observados)}, inicio, inicio + M(n)


@pytest.mark.parametrize("falhas,lacunas,meta,motivo", [
    (100, 0, True, None),                                   # exatamente 99% atende
    (101, 0, False, None),
    (0, 100, True, None),                                   # pior caso (lacuna = queda) ainda 99%
    (50, 51, None, "lacunas_de_monitoramento_impedem_concluir"),
    (0, 10000, None, "lacunas_de_monitoramento_impedem_concluir"),  # sem nenhuma medição
])
def test_meta_99_com_lacunas(falhas, lacunas, meta, motivo):
    minutos, inicio, corte = _minutos(10000, falhas, lacunas)
    r = _servico(minutos, inicio, corte, parcial=False)
    assert (r["meta_atendida"], r["motivo_meta_indefinida"]) == (meta, motivo)
    assert r["minutos_sem_medicao"] == lacunas
    if lacunas == 10000:
        assert r["percentual_observado"] is None and r["limite_inferior"] == 0.0
        assert r["limite_superior"] == 100.0


def test_mes_parcial_nao_conclui_e_denominador_zero(app):
    agora = datetime(2026, 11, 1, 3, 10, 30, tzinfo=UTC)  # 00:10:30 de 01/11 em São Paulo
    db.session.add_all([sonda("API", datetime(2026, 11, 1, 3, m, 5, tzinfo=UTC)) for m in range(10)])
    db.session.commit()
    r = disponibilidade_mensal("2026-11", agora=agora, intervalo_coleta_seg=30)
    api = r["servicos"]["API"]
    assert r["mes_parcial"] and (api["minutos_esperados"], api["minutos_observados"]) == (10, 10)
    assert (api["meta_atendida"], api["motivo_meta_indefinida"]) == (None, "mes_parcial")
    assert r["servicos"]["PAINEL"]["minutos_sem_medicao"] == 10  # PAINEL sem sondagem
    inicio = disponibilidade_mensal("2026-11", agora=datetime(2026, 11, 1, 3, 0, 20, tzinfo=UTC),
                                    intervalo_coleta_seg=30)
    assert inicio["servicos"]["API"]["motivo_meta_indefinida"] == "periodo_sem_minutos_esperados"
    assert inicio["servicos"]["COLETA"]["motivo_meta_indefinida"] == "periodo_sem_ciclos_esperados"
    with pytest.raises(ValueError):
        disponibilidade_mensal("2026-12", agora=agora)


def test_mes_usa_fuso_de_sao_paulo():
    inicio, fim = limites_mes("2026-11")
    assert (inicio, fim) == (datetime(2026, 11, 1, 3, tzinfo=UTC), datetime(2026, 12, 1, 3, tzinfo=UTC))


def test_falha_da_fonte_na_coleta_nao_apaga_queda_da_api(app):
    agora = datetime(2026, 11, 1, 3, 10, tzinfo=UTC)
    db.session.add_all([sonda("API", datetime(2026, 11, 1, 3, m, tzinfo=UTC), "INDISPONIVEL", "HTTP_ERRO")
                        for m in range(10)])
    db.session.add(ExecucaoColeta(fonte="OLHO_VIVO", prevista_em=datetime(2026, 11, 1, 3, 1, tzinfo=UTC),
                                  iniciada_em=datetime(2026, 11, 1, 3, 1, tzinfo=UTC), estado="FALHA",
                                  erro="SPTRANS_HTTP_503"))
    db.session.commit()
    r = disponibilidade_mensal("2026-11", agora=agora, intervalo_coleta_seg=30)
    assert r["servicos"]["API"]["minutos_falha"] == 10
    assert r["servicos"]["API"]["incidentes"]["itens"][0]["minutos"] == 10
    assert r["servicos"]["COLETA"]["falhas_fonte_excluidas"] == 1


# --- US30-T03A: GET /saude ------------------------------------------------------------

def test_rota_separa_servicos_sem_sondar(app, monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: pytest.fail("a rota não pode sondar"))
    monkeypatch.setattr("app.api.saude.agora_utc", lambda: datetime(2026, 11, 1, 3, 10, 30, tzinfo=UTC))
    db.session.add_all([sonda("API", datetime(2026, 11, 1, 3, m, tzinfo=UTC)) for m in range(8)])
    db.session.add(sonda("API", datetime(2026, 11, 1, 3, 9, tzinfo=UTC), "INDISPONIVEL", "TIMEOUT"))
    db.session.add(LacunaColeta(id="lac", inicio=datetime(2026, 11, 1, 3, 2, tzinfo=UTC), fim=None,
                                causa="SPTransTimeoutError", fonte="OLHO_VIVO"))
    db.session.commit()
    r = app.test_client().get("/saude")
    assert r.status_code == 200
    corpo = r.json
    assert set(corpo["servicos"]) == {"API", "PAINEL", "COLETA"} and corpo["mes"] == "2026-11"
    api = corpo["servicos"]["API"]
    assert api["ultima_medicao"]["estado"] == "INDISPONIVEL" and api["ultima_medicao"]["no_periodo"]
    assert (api["minutos_sucesso"], api["minutos_falha"], api["minutos_sem_medicao"]) == (8, 1, 1)
    assert api["lacunas_monitoramento"]["itens"][0]["inicio"] == "2026-11-01T03:08:00+00:00"
    assert corpo["servicos"]["PAINEL"]["ultima_medicao"] is None
    assert corpo["servicos"]["COLETA"]["incidentes"]["itens"][0]["estado_no_corte"] == "ABERTA"
    assert corpo["mes_parcial"] is True and corpo["limites"]
    assert app.test_client().get("/api/saude?mes=2026-11").json == corpo
    assert app.test_client().get("/health").json == {"status": "ok"}


@pytest.mark.parametrize("consulta", ["mes=2026-13", "mes=2999-01", "mes=x", "outro=1", "mes=2026-11&mes=2026-10"])
def test_rota_valida_parametros(app, consulta):
    assert app.test_client().get(f"/saude?{consulta}").status_code == 400


def test_rota_nao_expoe_detalhe_do_banco(app, monkeypatch):
    def falhar(*_, **__):
        raise OperationalError("SELECT", {}, Exception("senha=segredo host=aiven"))

    monkeypatch.setattr("app.api.saude.consultar_saude", falhar)
    r = app.test_client().get("/saude")
    assert r.status_code == 503 and "segredo" not in r.get_data(as_text=True)


def test_migracao_us30_corresponde_ao_modelo_e_gera_ddl_mysql():
    import importlib.util
    import io
    from pathlib import Path

    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine

    arquivo = Path(__file__).resolve().parents[2] / "migrations/versions/us30_0026_verificacoes_saude.py"
    spec = importlib.util.spec_from_file_location("migracao_us30", arquivo)
    migracao = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migracao)
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as conexao:
            db.metadata.create_all(conexao, tables=[t for t in db.metadata.sorted_tables
                                                    if t.name != "verificacoes_saude"])
            contexto = MigrationContext.configure(conexao)
            with Operations.context(contexto):
                migracao.upgrade()
            assert compare_metadata(contexto, db.metadata) == []
    finally:
        engine.dispose()
    saida = io.StringIO()
    offline = MigrationContext.configure(dialect_name="mysql", opts={"as_sql": True, "output_buffer": saida})
    with Operations.context(offline):
        migracao.upgrade()
    sql = saida.getvalue()
    assert "CREATE TABLE verificacoes_saude" in sql and "DATETIME(6)" in sql and "ENGINE=InnoDB" in sql
    with pytest.raises(RuntimeError):
        migracao.downgrade()
