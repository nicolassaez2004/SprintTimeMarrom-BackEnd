"""US11-T02: orçamento único de 60 consultas/minuto por IP, persistido em SQL."""

import hashlib
from dataclasses import dataclass
from ipaddress import ip_address, ip_network

from flask import current_app
from sqlalchemy import case, func, select, text
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError

from app import db
from app.models import LimiteConsultaIP

LIMITE = 60
JANELA_SEGUNDOS = 60


class IdentidadeIPInvalida(ValueError):
    pass


def _ip(valor):
    try:
        endereco = ip_address(valor)
        return getattr(endereco, "ipv4_mapped", None) or endereco
    except (ValueError, TypeError) as erro:
        raise IdentidadeIPInvalida("Não foi possível identificar o IP da conexão.") from erro


def configurar_limites(app):
    valor = app.config.get("INDICADORES_TRUSTED_PROXY_CIDRS") or ""
    redes = []
    for cidr in valor.split(","):
        if not cidr.strip():
            continue
        try:
            rede = ip_network(cidr.strip(), strict=False)
        except ValueError as erro:
            raise ValueError("INDICADORES_TRUSTED_PROXY_CIDRS contém uma rede inválida") from erro
        if rede.prefixlen == 0:
            raise ValueError("Um proxy confiável precisa de uma rede restrita; /0 não é permitido")
        redes.append(rede)
    app.extensions["indicadores_proxies_confiaveis"] = tuple(redes)
    if app.config.get("LIMITES_RELOGIO") is not None and not app.testing:
        raise ValueError("LIMITES_RELOGIO só pode ser substituído nos testes")


def identificar_ip(requisicao):
    # ProxyFix pode ter reescrito REMOTE_ADDR. Sua cópia original vem do
    # ambiente WSGI, não de um cabeçalho controlado pelo cliente.
    original = requisicao.environ.get("werkzeug.proxy_fix.orig", {})
    remoto = original.get("REMOTE_ADDR", requisicao.remote_addr) if isinstance(original, dict) else requisicao.remote_addr
    endereco = _ip(remoto)
    redes = current_app.extensions["indicadores_proxies_confiaveis"]

    def confiavel(ip):
        return any(ip.version == rede.version and ip in rede for rede in redes)

    # Só atravessa os saltos comprovadamente confiáveis, da direita para a
    # esquerda. Um prefixo arbitrário do cliente não vira a identidade.
    if confiavel(endereco):
        encaminhados = requisicao.headers.get("X-Forwarded-For", "")
        if encaminhados:
            for candidato in reversed(encaminhados.split(",")):
                if not confiavel(endereco):
                    break
                endereco = _ip(candidato.strip())
    return str(endereco)


@dataclass(frozen=True)
class ConsumoLimite:
    permitido: bool
    restantes: int
    reinicia_em: int
    tentar_em: int

    def cabecalhos(self):
        return {"X-RateLimit-Limit": str(LIMITE), "X-RateLimit-Remaining": str(self.restantes),
                "X-RateLimit-Reset": str(self.reinicia_em)}


def comando_consumo(dialeto, chave, janela):
    tabela = LimiteConsultaIP.__table__
    novo = {"chave_ip": chave, "janela": janela, "tentativas": 1}
    reiniciar = tabela.c.janela < janela
    if dialeto in ("mysql", "mariadb"):
        # MySQL aplica atribuições na ordem: comparar a janela anterior antes
        # de gravar a nova. LEAST satura os pedidos bloqueados, sem overflow.
        return mysql_insert(tabela).values(**novo).on_duplicate_key_update([
            ("tentativas", case((reiniciar, 1), else_=func.least(tabela.c.tentativas + 1, LIMITE + 1))),
            ("janela", func.greatest(tabela.c.janela, janela)),
        ])
    if dialeto == "sqlite":
        return sqlite_insert(tabela).values(**novo).on_conflict_do_update(
            index_elements=[tabela.c.chave_ip], set_={
                "tentativas": case((reiniciar, 1), else_=func.min(tabela.c.tentativas + 1, LIMITE + 1)),
                "janela": func.max(tabela.c.janela, janela),
            })
    raise RuntimeError("O limitador suporta MySQL/MariaDB e SQLite")


def consumir_limite(ip):
    uri = db.engine.url
    if db.engine.dialect.name == "sqlite" and not current_app.testing and (
        not uri.database or uri.database == ":memory:" or uri.database.startswith("file::memory:")
        or uri.query.get("mode") == "memory"
    ):
        raise SQLAlchemyError("Contador em memória só é permitido em testes isolados")
    chave = hashlib.sha256(_ip(ip).compressed.encode("ascii")).hexdigest()
    # Transação própria, independente da consulta: um rollback posterior da
    # API não devolve o orçamento. UPSERT e SELECT usam a mesma conexão/lock.
    with db.engine.begin() as conexao:
        relogio = current_app.config.get("LIMITES_RELOGIO")
        if relogio is not None:
            agora = int(relogio())
        elif conexao.dialect.name in ("mysql", "mariadb"):
            agora = int(conexao.scalar(text("SELECT UNIX_TIMESTAMP()")))
        else:
            agora = int(conexao.scalar(text("SELECT CAST(strftime('%s', 'now') AS INTEGER)")))
        janela = agora // JANELA_SEGUNDOS
        conexao.execute(comando_consumo(conexao.dialect.name, chave, janela))
        gravada, tentativas = conexao.execute(select(LimiteConsultaIP.janela, LimiteConsultaIP.tentativas)
            .where(LimiteConsultaIP.chave_ip == chave)).one()
    reinicia_em = (gravada + 1) * JANELA_SEGUNDOS
    return ConsumoLimite(tentativas <= LIMITE, max(0, LIMITE - tentativas), reinicia_em,
                         max(1, reinicia_em - agora))
