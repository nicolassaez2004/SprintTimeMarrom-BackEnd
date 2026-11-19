"""Política de retentativas; sem HTTP, SQL ou criação de processos."""

from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError, TimeoutError

from app.integrations.sptrans import (
    SPTransConexaoError,
    SPTransHTTPError,
    SPTransRespostaInvalidaError,
)

MAX_TENTATIVAS = 6  # inicial + cinco novas tentativas
ESPERAS = (1, 2, 4, 8, 16)


def temporaria(erro):
    if isinstance(erro, DBAPIError) and erro.connection_invalidated:
        return True
    # Página de manutenção/JSON truncado costuma ser passageiro na Olho Vivo.
    if isinstance(erro, (SPTransConexaoError, SPTransRespostaInvalidaError)):
        return True
    if isinstance(erro, SPTransHTTPError):
        return erro.status_code in (408, 425, 429) or 500 <= erro.status_code <= 599
    if isinstance(erro, (InterfaceError, TimeoutError)):
        return True
    if isinstance(erro, OperationalError):
        argumentos = getattr(erro.orig, "args", ())
        codigo = argumentos[0] if argumentos else None
        if isinstance(codigo, int):
            return codigo in (
                1040,
                1053,
                1158,
                1159,
                1160,
                1161,
                1203,
                1205,
                1213,
                2002,
                2003,
                2006,
                2013,
                2055,
            )
        return True  # adaptadores sem códigos numéricos (por exemplo SQLite nos testes)
    return isinstance(erro, DBAPIError) and erro.connection_invalidated


def causa_segura(erro):
    """Nunca serializa mensagens de exceção, SQL, cookies, token ou credenciais."""
    if isinstance(erro, SPTransHTTPError):
        return f"SPTRANS_HTTP_{erro.status_code}"
    return type(erro).__name__[:128]
