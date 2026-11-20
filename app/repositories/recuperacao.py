"""Snapshot lógico MySQL: DDL + linhas tipadas, na mesma transação InnoDB.

O formato JSONL é próprio, versionado; não deve ser importado no Workbench
como SQL. Ao contrário de consultas independentes a COUNT e mysqldump,
contagens e hashes aqui pertencem ao MESMO snapshot das linhas copiadas.
"""

import base64
import hashlib
import json
import re
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pymysql

DDL_SQL_MODE = "STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION"


def identificador(nome):
    return "`" + nome.replace("`", "``") + "`"


def codificar(valor):
    if isinstance(valor, bytes):
        return {"tipo": "bytes", "valor": base64.b64encode(valor).decode("ascii")}
    if isinstance(valor, (datetime, date, time)):
        return {"tipo": type(valor).__name__, "valor": valor.isoformat()}
    if isinstance(valor, Decimal):
        return {"tipo": "decimal", "valor": str(valor)}
    if isinstance(valor, timedelta):
        return {"tipo": "timedelta", "valor": valor // timedelta(microseconds=1)}
    if valor is None or isinstance(valor, (str, int, float)):
        return valor
    raise TypeError("Tipo SQL não coberto pelo formato de backup")


def decodificar(valor):
    if not isinstance(valor, dict):
        return valor
    tipo, dado = valor["tipo"], valor["valor"]
    if tipo == "bytes":
        return base64.b64decode(dado, validate=True)
    if tipo == "timedelta":
        return timedelta(microseconds=dado)
    conversores = {"datetime": datetime.fromisoformat, "date": date.fromisoformat,
                   "time": time.fromisoformat, "decimal": Decimal}
    return conversores[tipo](dado)


def serializar(linha):
    return json.dumps([codificar(v) for v in linha], ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"


def metadados(conexao):
    with conexao.cursor() as c:
        c.execute("SELECT VERSION(), DATABASE(), @@SESSION.sql_mode")
        versao, banco, sql_mode = c.fetchone()
        if not versao.startswith("8.") or "MariaDB" in versao:
            raise ValueError("Backup validado para MySQL 8.x")
        c.execute("SHOW FULL TABLES")
        tabelas = c.fetchall()
        if any(tipo != "BASE TABLE" for _, tipo in tabelas):
            raise ValueError("Views exigem ampliar o formato de backup antes de prosseguir")
        c.execute("SELECT COUNT(*) FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=DATABASE()")
        if c.fetchone()[0]:
            raise ValueError("Triggers não cobertos pelo formato de backup")
        for objeto, coluna in (("ROUTINES", "ROUTINE_SCHEMA"), ("EVENTS", "EVENT_SCHEMA")):
            c.execute(f"SELECT COUNT(*) FROM information_schema.{objeto} WHERE {coluna}=DATABASE()")
            if c.fetchone()[0]:
                raise ValueError("Rotinas/eventos não cobertos pelo formato de backup")
        resultado = []
        for nome, _ in sorted(tabelas):
            qnome = identificador(nome)
            # Adquire MDL até o fim da transação: ALTER/DROP concorrentes
            # aguardam. INSERT/UPDATE InnoDB continuam no snapshot MVCC.
            c.execute(f"SELECT * FROM {qnome} LIMIT 0")
            c.execute("SELECT ENGINE FROM information_schema.TABLES "
                      "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", (nome,))
            if c.fetchone()[0] != "InnoDB":
                raise ValueError("Todas as tabelas devem ser InnoDB")
            # ANSI oculta opções MySQL no SHOW CREATE (engine/charset etc.).
            # Capturar DDL completo em modo canônico, preservando o modo
            # original no manifesto para a recuperação da configuração.
            c.execute("SET SESSION sql_mode=%s", (DDL_SQL_MODE,))
            try:
                c.execute(f"SHOW CREATE TABLE {qnome}")
                ddl = c.fetchone()[1]
            finally:
                c.execute("SET SESSION sql_mode=%s", (sql_mode,))
            c.execute(f"SHOW FULL COLUMNS FROM {qnome}")
            campos = c.fetchall()
            if any("VIRTUAL GENERATED" in campo[6] or "STORED GENERATED" in campo[6] for campo in campos):
                raise ValueError("Colunas geradas exigem suporte explícito")
            c.execute(f"SHOW INDEX FROM {qnome} WHERE Key_name='PRIMARY'")
            pk = [r[4] for r in sorted(c.fetchall(), key=lambda r: r[3])]
            if not pk:
                raise ValueError("Toda tabela deve ter chave primária para conferir os hashes")
            c.execute("SELECT CONSTRAINT_NAME,COLUMN_NAME,REFERENCED_TABLE_NAME,"
                      "REFERENCED_COLUMN_NAME,REFERENCED_TABLE_SCHEMA FROM "
                      "information_schema.KEY_COLUMN_USAGE WHERE TABLE_SCHEMA=DATABASE() "
                      "AND TABLE_NAME=%s AND REFERENCED_TABLE_NAME IS NOT NULL "
                      "ORDER BY CONSTRAINT_NAME,ORDINAL_POSITION", (nome,))
            fks = {}
            for restricao, coluna, pai, coluna_pai, esquema_pai in c.fetchall():
                if esquema_pai != banco:
                    raise ValueError("Chave estrangeira fora da base não pode ser isolada")
                fk = fks.setdefault(restricao, {"tabela_pai": pai, "colunas": [], "colunas_pai": []})
                fk["colunas"].append(coluna)
                fk["colunas_pai"].append(coluna_pai)
            resultado.append({"nome": nome, "ddl": ddl, "pk": pk,
                              "colunas": [r[0] for r in campos],
                              "tipos": [r[1] for r in campos], "fks": list(fks.values())})
        return {"mysql_versao": versao, "banco": banco, "sql_mode": sql_mode,
                "ddl_sql_mode": DDL_SQL_MODE, "tabelas": resultado}


def linhas(conexao, tabela):
    ordem = ",".join(map(identificador, tabela["pk"]))
    with conexao.cursor(pymysql.cursors.SSCursor) as c:
        c.execute(f"SELECT * FROM {identificador(tabela['nome'])} ORDER BY {ordem}")
        while lote := c.fetchmany(1000):
            yield from lote


def resumo(conexao, tabela):
    digest = hashlib.sha256()
    quantidade = 0
    for linha in linhas(conexao, tabela):
        digest.update(serializar(linha))
        quantidade += 1
    return {"registros": quantidade, "sha256_linhas": digest.hexdigest()}


def inserir(conexao, tabela, lote):
    espaciais = {"geometry", "point", "linestring", "polygon", "multipoint",
                 "multilinestring", "multipolygon", "geometrycollection"}
    partes = ["ST_GeomFromWKB(%s,%s)" if t.lower() in espaciais else "%s"
              for t in tabela["tipos"]]
    valores = []
    for linha in lote:
        parametros = []
        for tipo, valor in zip(tabela["tipos"], linha, strict=True):
            if tipo.lower() in espaciais:
                # MySQL representa geometria como SRID little-endian + WKB.
                parametros.extend((valor[4:], int.from_bytes(valor[:4], "little"))
                                  if valor is not None else (None, 0))
            else:
                parametros.append(valor)
        valores.append(parametros)
    colunas = ",".join(map(identificador, tabela["colunas"]))
    sql = f"INSERT INTO {identificador(tabela['nome'])} ({colunas}) VALUES ({','.join(partes)})"
    with conexao.cursor() as c:
        c.executemany(sql, valores)


def conferir_relacionamentos(conexao, tabelas):
    total = 0
    with conexao.cursor() as c:
        for tabela in tabelas:
            for fk in tabela["fks"]:
                presente = " AND ".join(f"c.{identificador(n)} IS NOT NULL" for n in fk["colunas"])
                igualdade = " AND ".join(
                    f"c.{identificador(a)}=p.{identificador(b)}"
                    for a, b in zip(fk["colunas"], fk["colunas_pai"], strict=True))
                c.execute(f"SELECT COUNT(*) FROM {identificador(tabela['nome'])} c "
                          f"WHERE {presente} AND NOT EXISTS (SELECT 1 FROM "
                          f"{identificador(fk['tabela_pai'])} p WHERE {igualdade})")
                if c.fetchone()[0]:
                    raise ValueError("Restauração contém referências órfãs")
                total += 1
    return total


def esquema_normalizado(ddl):
    # O contador pode avançar enquanto o snapshot lê linhas anteriores.
    ddl = re.sub(r" AUTO_INCREMENT=\d+", "", ddl)
    # 8.0 explicita CHARACTER SET no campo, 8.4 pode inferi-lo da mesma
    # COLLATE. Remover só a redundância, mantendo a collation exata.
    return re.sub(r"CHARACTER SET (\w+) (?=COLLATE \1_)", "", ddl)
