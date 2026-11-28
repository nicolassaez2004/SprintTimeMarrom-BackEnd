"""US31-T02A: lote pequeno do gerador de histórico com chaves estrangeiras habilitadas.

SQLite em arquivo temporário com ``PRAGMA foreign_keys=ON`` (padrão). O mesmo teste roda no
MySQL local isolado com ``US31_MYSQL_TEST_URL=mysql+pymysql://...@127.0.0.1/us31_teste_*``.
"""
import hashlib
import json
import os

import pytest
from sqlalchemy import event, text

from app import create_app, db
from scripts.gerar_historico import Parametros, gerar

LOTE = Parametros(dias=0.25, veiculos=30, linhas=5, lacunas_por_dia=2, duracao_lacuna_min=20, semente=31)
# Contagens esperadas registradas para este lote (recriar com os mesmos parâmetros dá o mesmo).
ESPERADO = {"versoes_gtfs": 1, "linhas": 5, "veiculos": 30, "execucoes_coleta": 720, "lacunas_coleta": 2,
            "indicadores_operacionais": 480}


def _app(url, tmp_path):
    aplicacao = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": url, "MYSQL_SSL_CA": None})
    aplicacao.instance_path = str(tmp_path)
    return aplicacao


@pytest.fixture(params=["sqlite", "mysql"])
def base(request, tmp_path):
    if request.param == "sqlite":
        url = f"sqlite:///{tmp_path / 'historico.db'}"
    else:
        url = os.getenv("US31_MYSQL_TEST_URL")
        if not url or "/us31_teste" not in url:
            pytest.skip("MySQL opt-in: defina US31_MYSQL_TEST_URL (base us31_teste_*)")
    aplicacao = _app(url, tmp_path)
    with aplicacao.app_context():
        if request.param == "sqlite":
            event.listen(db.engine, "connect", lambda c, _: c.execute("PRAGMA foreign_keys=ON"))
        db.drop_all()
        db.create_all()
        yield request.param
        db.session.remove()
        if request.param == "mysql":
            db.drop_all()
        db.engine.dispose()


def _conteudo():
    """Hash de todas as linhas geradas, em ordem estável (prova de que o lote é recriável)."""
    digest = hashlib.sha256()
    with db.engine.connect() as c:
        for tabela, ordem in (("execucoes_coleta", "id"), ("lacunas_coleta", "id"), ("registros_gps", "id"),
                              ("observacoes_tratadas", "id"), ("indicadores_operacionais", "id")):
            for linha in c.execute(text(f"SELECT * FROM {tabela} ORDER BY {ordem}")):
                digest.update(json.dumps([str(v) for v in linha]).encode())
    return digest.hexdigest()


def test_lote_respeita_relacoes_e_contagens(base):
    manifesto = gerar(db.engine, LOTE, permitir=True)
    c = manifesto["contagens"]
    assert {k: c[k] for k in ESPERADO} == ESPERADO
    with db.engine.connect() as cx:
        for tabela, n in c.items():
            assert cx.execute(text(f"SELECT COUNT(*) FROM {tabela}")).scalar() == n, tabela
        if base == "sqlite":
            assert cx.execute(text("PRAGMA foreign_key_check")).fetchall() == []
        # Lacunas: ciclos FALHA, sem nenhum bruto (o intervalo sem cobertura não é preenchido).
        falhas = cx.execute(text("SELECT COUNT(*) FROM execucoes_coleta WHERE lacuna_id IS NOT NULL")).scalar()
        assert falhas > 0 and cx.execute(text(
            "SELECT COUNT(*) FROM registros_gps r JOIN execucoes_coleta e ON e.id = r.execucao_id "
            "WHERE e.lacuna_id IS NOT NULL")).scalar() == 0
        # Cada bruto tem exatamente uma observação tratada, ligada à linha versionada.
        assert cx.execute(text("SELECT COUNT(*) FROM registros_gps r LEFT JOIN observacoes_tratadas o "
                               "ON o.bruto_id = r.id WHERE o.id IS NULL")).scalar() == 0
        # Novos ÷ recebidos perto do medido na coleta real (≈ 0,70).
        recebidos, novos = cx.execute(text("SELECT SUM(recebidos), SUM(novos_persistidos) FROM execucoes_coleta "
                                           "WHERE estado = 'OK'")).one()
        assert 0.65 < novos / recebidos < 0.75
        # Marcado como sintético em toda parte.
        assert cx.execute(text("SELECT COUNT(DISTINCT versao_conjunto) FROM indicadores_operacionais")).scalar() == 1
        assert cx.execute(text("SELECT COUNT(*) FROM observacoes_tratadas WHERE versao_correspondencia "
                               "<> 'sintetico-us31-v1'")).scalar() == 0
    assert manifesto["sintetico"] is True and manifesto["ids_brutos"][1] - manifesto["ids_brutos"][0] + 1 == c["registros_gps"]


def test_mesma_semente_recria_exatamente_o_mesmo_lote(base, tmp_path):
    primeiro = gerar(db.engine, LOTE, permitir=True)
    conteudo = _conteudo()
    db.drop_all()
    db.create_all()
    segundo = gerar(db.engine, LOTE, permitir=True)
    assert (segundo["contagens"], segundo["hash_parametros"]) == (primeiro["contagens"], primeiro["hash_parametros"])
    assert _conteudo() == conteudo


def test_conferencia_aprova_o_lote_e_aponta_o_que_diverge(base):
    from scripts.gerar_historico import conferir

    manifesto = gerar(db.engine, LOTE, permitir=True)
    conferencia = conferir(db.engine, manifesto)
    assert conferencia["ok"], {k: v for k, v in conferencia["itens"].items() if not v["ok"]}
    with db.engine.begin() as cx:  # uma observação sem a marca sintética: a conferência acusa
        alvo = cx.execute(text("SELECT MIN(id) FROM observacoes_tratadas")).scalar()
        cx.execute(text("UPDATE observacoes_tratadas SET versao_correspondencia = 'real' WHERE id = :i"), {"i": alvo})
    itens = conferir(db.engine, manifesto)["itens"]
    assert not itens["sintetico.tratadas_fora_da_versao"]["ok"] and itens["contagem.registros_gps"]["ok"]


def test_recusa_mysql_sem_permissao_ou_remoto():
    from sqlalchemy import create_engine

    with pytest.raises(ValueError):
        gerar(create_engine("mysql+pymysql://u:p@127.0.0.1/us31_x"), LOTE)
    with pytest.raises(ValueError):
        gerar(create_engine("mysql+pymysql://u:p@banco.aivencloud.com/sptrans_dev"), LOTE, permitir=True)
