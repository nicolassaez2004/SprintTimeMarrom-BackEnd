"""Recupera somente em uma NOVA base local marrom_restore_*. Nunca DROP."""

import json
import re
import shutil
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from app.core.tempo import FUSO
from app.operacao.arquivos import caminho_interno, gravar_json
from app.operacao.backup import validar_conjunto
from app.repositories import recuperacao as repo
from app.storage import hash_arquivo


def proximo_ensaio(agora):
    local = agora.astimezone(FUSO)
    datas = [datetime(ano, mes, 20, 10, tzinfo=local.tzinfo)
             for ano in (local.year, local.year + 1) for mes in (5, 11)]
    return min(data for data in datas if data > local).date().isoformat()


def validar_alvo(url):
    url = make_url(url)
    if (url.drivername != "mysql+pymysql" or url.host not in {"127.0.0.1", "localhost", "::1"}
            or not re.fullmatch(r"marrom_restore_[a-z0-9_]{6,48}", url.database or "")):
        raise ValueError("Restauração exige MySQL local e NOVA base marrom_restore_<identificador>")
    # SQLAlchemy repassa a query ao driver e ela pode sobrescrever host/db,
    # ou carregar outro destino de um arquivo de opções. Validar só a parte
    # principal da URL não basta para garantir isolamento.
    if set(url.query) - {"charset"} or url.query.get("charset", "utf8mb4") != "utf8mb4":
        raise ValueError("A conexão de restauração só aceita o parâmetro charset=utf8mb4")
    return url


def restaurar_backup(pasta, url, destino_arquivos):
    iniciada, inicio = datetime.now(timezone.utc), time.monotonic()
    url = validar_alvo(url)
    pasta = Path(pasta).resolve()
    manifesto, esquema = validar_conjunto(pasta)  # Verificar antes de criar a base.
    destino = Path(destino_arquivos).resolve()
    if destino == pasta or destino.is_relative_to(pasta) or pasta.is_relative_to(destino):
        raise ValueError("Restauração de arquivos deve ficar separada do backup")
    if destino.exists():
        raise ValueError("Diretório de recuperação deve ser novo")
    engine = create_engine(url._replace(database=None), connect_args={
        "connect_timeout": 5, "read_timeout": 120, "write_timeout": 120,
    })
    criada = False
    try:
        with closing(engine.raw_connection()) as conn:
            with conn.cursor() as c:
                c.execute("SELECT COUNT(*) FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=%s", (url.database,))
                if c.fetchone()[0]:
                    raise ValueError("Base de destino já existe; nenhum dado foi sobrescrito")
                c.execute(f"CREATE DATABASE {repo.identificador(url.database)} "
                          "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
                criada = True
                c.execute(f"USE {repo.identificador(url.database)}")
                # SHOW CREATE TABLE respeita ANSI_QUOTES; a Aiven pode usar
                # aspas duplas onde o servidor local usa crases por padrão.
                c.execute("SET SESSION sql_mode=%s", (esquema["sql_mode"],))
                c.execute("SET SESSION time_zone='+00:00'")
                c.execute("SET SESSION FOREIGN_KEY_CHECKS=0")
                for tabela in esquema["tabelas"]:
                    c.execute(tabela["ddl"])
            for tabela in esquema["tabelas"]:
                lote = []
                with caminho_interno(pasta, tabela["dados"]).open(encoding="utf-8") as stream:
                    for linha in stream:
                        lote.append([repo.decodificar(v) for v in json.loads(linha)])
                        if len(lote) == 250:
                            repo.inserir(conn, tabela, lote)
                            lote.clear()
                    if lote:
                        repo.inserir(conn, tabela, lote)
            with conn.cursor() as c:
                c.execute("SET SESSION FOREIGN_KEY_CHECKS=1")
            conferidas = repo.conferir_relacionamentos(conn, esquema["tabelas"])
            conn.commit()
            atual = repo.metadados(conn)
            atuais = {t["nome"]: t for t in atual["tabelas"]}
            resultados = {}
            for tabela in esquema["tabelas"]:
                if repo.esquema_normalizado(tabela["ddl"]) != repo.esquema_normalizado(atuais[tabela["nome"]]["ddl"]):
                    raise ValueError("DDL restaurado diverge do backup")
                resultado = repo.resumo(conn, tabela)
                if any(resultado[k] != tabela[k] for k in ("registros", "sha256_linhas")):
                    raise ValueError("Contagens ou hash das linhas restauradas divergem")
                resultados[tabela["nome"]] = resultado
        destino.mkdir(parents=True)
        for nome, info in manifesto["arquivos"].items():
            if not nome.startswith(("originais/", "configuracao/")) and nome != "configuracao.json":
                continue
            alvo = caminho_interno(destino, nome)
            alvo.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(caminho_interno(pasta, nome), alvo)
            if hash_arquivo(alvo) != (info["sha256"], info["bytes"]):
                raise ValueError("Arquivo restaurado divergente")
        # Conferir a conexão pela MESMA factory que a aplicação utiliza.
        from app import create_app, db
        app = create_app({"SQLALCHEMY_DATABASE_URI": url, "MYSQL_SSL_CA": None})
        from sqlalchemy import text
        with app.app_context():
            try:
                with db.engine.connect() as c:
                    revisoes = c.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
                    if c.execute(text("SELECT COUNT(*) FROM registros_gps")).scalar_one() != resultados["registros_gps"]["registros"]:
                        raise ValueError("Aplicação não encontrou os registros restaurados")
            finally:
                db.session.remove()
                db.engine.dispose()
        segundos = time.monotonic() - inicio
        relatorio = {
            "estado": "CONCLUIDO", "iniciada_em": iniciada.isoformat(),
            "finalizada_em": datetime.now(timezone.utc).isoformat(), "duracao_segundos": segundos,
            "ate_24_horas": segundos <= 86400, "banco_isolado": url.database,
            "origem_mysql": esquema["mysql_versao"], "destino_mysql": atual["mysql_versao"],
            "tabelas": resultados, "chaves_estrangeiras_conferidas": conferidas,
            "arquivos_originais": manifesto["originais"], "hashes_conferidos": True,
            "conexao_aplicacao": True, "revisoes_alembic": revisoes,
            "responsavel_ensaio": "Matheus (execução assistida pelo Codex)",
            "outro_integrante_reproduziu": False,
            "proxima_verificacao": proximo_ensaio(datetime.now(timezone.utc)), "periodicidade_ensaio": "SEMESTRAL",
            "backup_testado": pasta.name,
        }
        gravar_json(destino / "resultado-restauracao.json", relatorio)
        return relatorio
    except BaseException as erro:
        # Não apagar automaticamente o ambiente parcial de diagnóstico.
        gravar_json(destino.parent / (destino.name + ".falha.json"), {
            "estado": "FALHA", "tipo_erro": type(erro).__name__,
            "banco_isolado_criado": criada, "nenhuma_base_existente_sobrescrita": True,
        })
        raise
    finally:
        engine.dispose()
