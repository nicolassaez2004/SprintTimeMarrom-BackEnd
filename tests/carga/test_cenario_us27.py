"""US27: regras do ensaio de carga que não dependem de MySQL (o ensaio em si roda em tests/carga/cenario.py)."""
import math

import pytest

pytest.importorskip("psutil", reason="dependência dos ensaios: pip install -r requirements-medicao.txt")

from tests.carga.cenario import FalhaDoTeste, classificar, percentil, resumo, validar_url  # noqa: E402


def medicao(ms, status=200, usuario=0, db=None, app=None):
    servidor = {} if app is None else {"app": app, "db": db or 0, "sql": 3}
    return {"ms": ms, "status": status, "usuario": usuario, "bytes": 100, "servidor": servidor}


@pytest.mark.parametrize("url", [
    None, "", "sqlite:///x.db",
    "mysql+pymysql://u:p@empresa.aivencloud.com:3306/us27_carga",   # nunca a base compartilhada
    "mysql+pymysql://u:p@127.0.0.1:3306/sptrans_dev",               # nem base da equipe
])
def test_so_aceita_mysql_local_e_base_isolada(url):
    with pytest.raises(FalhaDoTeste):
        validar_url(url)


def test_aceita_base_isolada_local():
    assert validar_url("mysql+pymysql://u:p@127.0.0.1:3306/us27_carga_x").database == "us27_carga_x"


def test_percentil_nearest_rank():
    valores = list(range(1, 101))
    assert (percentil(valores, 95), percentil(valores, 99), percentil(valores, 50)) == (95, 99, 50)
    assert percentil([], 95) is None


def test_erro_nunca_conta_como_resposta_rapida():
    medicoes = [medicao(100) for _ in range(90)] + [medicao(5, status=503) for _ in range(10)]
    r = resumo(medicoes)
    assert r["erros"] == 10
    assert r["p95_ms_com_erros_como_infinito"] == math.inf
    assert r["status"] == {"200": 90, "503": 10}


def test_decomposicao_sql_python_e_fila():
    r = resumo([medicao(1000, db=600, app=900)] * 20)
    assert r["decomposicao_p95_ms"] == {"sql": 600, "python_montagem_e_json": 300, "fila_e_rede": 100}


def usuarios(n, **kwargs):
    return [medicao(usuario=i % 50, **kwargs) for i in range(n)]


def test_aprovado_so_com_metas_e_sem_erros():
    medicoes = usuarios(600, ms=500)
    assert classificar(resumo(medicoes), {"maximo": 10}, medicoes) == ("APROVADO", [])


def test_falha_do_requisito_por_percentil_ou_erro():
    lentas = usuarios(600, ms=2500)
    assert classificar(resumo(lentas), {"maximo": 10}, lentas)[0] == "FALHA_DO_REQUISITO"
    com_erro = usuarios(599, ms=100) + [medicao(50, status=500)]
    resultado, motivos = classificar(resumo(com_erro), {"maximo": 10}, com_erro)
    assert resultado == "FALHA_DO_REQUISITO" and "500" in motivos[0]


def test_falha_do_teste_quando_o_ensaio_nao_mede_o_prometido():
    pequena = usuarios(100, ms=100)
    assert classificar(resumo(pequena), {"maximo": 10}, pequena)[0] == "FALHA_DO_TESTE"
    cota = usuarios(599, ms=100) + [medicao(5, status=429)]
    assert classificar(resumo(cota), {"maximo": 10}, cota)[0] == "FALHA_DO_TESTE"
    poucos = [medicao(100, usuario=i % 10) for i in range(600)]
    assert classificar(resumo(poucos), {"maximo": 10}, poucos)[0] == "FALHA_DO_TESTE"


def test_timeouts_nao_viram_falha_do_teste_por_amostra_pequena():
    travado = usuarios(288, ms=30000, status="TimeoutError")
    resultado, motivos = classificar(resumo(travado), {"maximo": 10}, travado)
    assert resultado == "FALHA_DO_REQUISITO"
    assert not any(m.startswith("amostra menor") for m in motivos)
