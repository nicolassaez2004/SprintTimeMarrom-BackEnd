"""Vinte referências manuais CONTROLADAS; não há aceite institucional implícito."""

from collections import Counter
from hashlib import sha256
import json

import pytest

from app import create_app, db
from scripts.validar_us15_t04 import CADERNO, RAIZ, executar, exigir_sqlite_em_memoria, hash_texto


@pytest.fixture(scope="module")
def conferencia():
    return executar()


@pytest.mark.parametrize("identificador", [f"C{i:02}" for i in range(1, 21)])
def test_caso_confere_com_referencia_manual(conferencia, identificador):
    caso = next(c for c in conferencia["casos"] if c["id"] == identificador)
    divergencias = [c for c in caso.get("comparacoes", []) if not c["confere"]]
    passagens = [p for p in caso.get("passagens", []) if not p["confere"]]
    assert caso["status"] == "CONFERIU_EXEMPLO", {
        "caso": identificador, "erro": caso.get("erro"),
        "comparacoes": divergencias, "passagens": passagens,
    }


def test_vinte_conferencias_tecnicas_nao_equivalem_ao_aceite(conferencia):
    assert conferencia["resumo"] == {"CONFERIU_EXEMPLO": 20}
    assert conferencia["pronto"] is False
    assert conferencia["aceite_institucional"] == "PENDENTE"
    assert all(c["aceite_institucional"] == "PENDENTE" for c in conferencia["casos"])
    assert Counter(c["escala"] for c in conferencia["casos"]) == {
        "janela": 5, "faixa": 5, "dia": 5, "mes": 5,
    }


def test_evidencia_guardada_identifica_o_caderno_e_nao_inventa_aprovacao(tmp_path):
    evidencia = json.loads((RAIZ / "docs/evidencias/US15-T04-conferencia.json").read_text(encoding="utf-8"))
    assert evidencia["sha256_caderno"] == hash_texto(CADERNO)
    lf, crlf = tmp_path / "lf.txt", tmp_path / "crlf.txt"
    lf.write_bytes(b"exemplo\n")
    crlf.write_bytes(b"exemplo\r\n")
    assert hash_texto(lf) == hash_texto(crlf) == sha256(b"exemplo\n").hexdigest()
    assert evidencia["pronto"] is False and evidencia["aceite_institucional"] == "PENDENTE"
    assert evidencia["origem"] == "SINTETICA_CONTROLADA"
    assert len(evidencia["casos"]) == 20


def test_guardia_rejeita_qualquer_base_persistente_antes_de_criar_tabelas(tmp_path):
    destino = tmp_path / "nao-deve-ser-criada.sqlite"
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///" + destino.as_posix(),
                      "MYSQL_SSL_CA": None})
    with app.app_context():
        try:
            with pytest.raises(RuntimeError, match="SQLite em memória"):
                exigir_sqlite_em_memoria()
            assert not destino.exists()
        finally:
            db.engine.dispose()
