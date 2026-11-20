import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.operacao.arquivos import caminho_interno, gravar_json
from app.operacao.backup import validar_conjunto
from app.operacao.restauracao import proximo_ensaio, validar_alvo
from app.repositories.recuperacao import decodificar, esquema_normalizado, serializar
from app.storage import hash_arquivo


@pytest.mark.parametrize("valor", [None, "texto\ncom ' aspas", b"\x00\xff", 1, 1.00000001,
                                  Decimal("1.2300"), date(2026, 11, 20),
                                  datetime(2026, 11, 20, 4, 5, 6, 123456),
                                  timedelta(days=-1, microseconds=123)])
def test_formato_preserva_tipos_null_e_precisao(valor):
    assert decodificar(json.loads(serializar([valor]))[0]) == valor


@pytest.mark.parametrize("url", [
    "mysql+pymysql://u:p@aiven.example/marrom_restore_teste01",
    "mysql+pymysql://u:p@127.0.0.1/sptrans_dev",
    "mysql+pymysql://u:p@127.0.0.1/sptrans_test",
    "mysql+pymysql://u:p@127.0.0.1/defaultdb",
    "sqlite:///marrom_restore_teste01",
    "mysql+pymysql://u:p@127.0.0.1/marrom_restore_teste01?host=aiven.example",
    "mysql+pymysql://u:p@127.0.0.1/marrom_restore_teste01?db=sptrans_dev",
    "mysql+pymysql://u:p@127.0.0.1/marrom_restore_teste01?read_default_file=outro.ini",
])
def test_nunca_restaura_em_producao_ou_teste_compartilhado(url):
    with pytest.raises(ValueError):
        validar_alvo(url)


def test_caminho_nao_escapa_do_backup(tmp_path):
    with pytest.raises(ValueError):
        caminho_interno(tmp_path, "../segredo")


def test_ddl_80_84_aceita_charset_redundante_sem_esconder_collation_diferente():
    ddl80 = "codigo varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin"
    ddl84 = "codigo varchar(64) COLLATE utf8mb4_0900_bin"
    assert esquema_normalizado(ddl80) == esquema_normalizado(ddl84)
    assert esquema_normalizado(ddl80) != esquema_normalizado(ddl84.replace("0900_bin", "unicode_ci"))


@pytest.mark.parametrize("instante,proximo", [
    (datetime(2026, 11, 20, 17, tzinfo=timezone.utc), "2027-05-20"),
    (datetime(2027, 5, 20, 13, tzinfo=timezone.utc), "2027-11-20"),
    (datetime(2027, 11, 20, 13, tzinfo=timezone.utc), "2028-05-20"),
])
def test_proximo_ensaio_nao_fica_preso_na_data_inicial(instante, proximo):
    assert proximo_ensaio(instante) == proximo


def test_corrupcao_e_parcial_impedem_restauracao(tmp_path):
    pasta = tmp_path / "backup"
    pasta.mkdir()
    gravar_json(pasta / "esquema.json", {"tabelas": [], "sql_mode": "ANSI_QUOTES",
                                        "ddl_sql_mode": "STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION"})
    gravar_json(pasta / "configuracao.json", {})
    manifesto = {"estado": "CONCLUIDO", "formato": "time-marrom-mysql-jsonl-v2", "arquivos": {
        n: {"sha256": hash_arquivo(pasta / n)[0], "bytes": (pasta / n).stat().st_size}
        for n in ["esquema.json", "configuracao.json"]}}
    gravar_json(pasta / "manifesto.json", manifesto)
    (pasta / "manifesto.sha256").write_text(hash_arquivo(pasta / "manifesto.json")[0])
    assert validar_conjunto(pasta)[0]["estado"] == "CONCLUIDO"
    (pasta / "esquema.json").write_text("corrompido")
    with pytest.raises(ValueError, match="corrompido"):
        validar_conjunto(pasta)
    with pytest.raises(ValueError, match="parcial"):
        validar_conjunto(tmp_path / ".backup.partial")
