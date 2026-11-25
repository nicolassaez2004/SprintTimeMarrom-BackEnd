"""Utilitários compartilhados pelas rotas: erro JSON, parâmetro inválido e limite por IP."""

from flask import current_app, g, request
from sqlalchemy.exc import SQLAlchemyError

from app.core.limites import IdentidadeIPInvalida, consumir_limite, identificar_ip


class ParametroInvalido(ValueError):
    def __init__(self, campo, mensagem):
        self.campo = campo
        super().__init__(mensagem)


def erro_json(codigo, mensagem, status, campo=None, **extra):
    detalhe = {"codigo": codigo, "mensagem": mensagem, **extra}
    if campo is not None:
        detalhe["campo"] = campo
    return {"erro": detalhe}, status


def limitar_por_ip(blueprint, *, recurso, mensagem_excedido, endpoints=None):
    """Aplica o limite compartilhado de consultas por IP (US11-T02) ao blueprint.

    `endpoints` restringe o limite a rotas específicas; None limita todas.
    """

    @blueprint.before_request
    def _consumir():
        if request.method not in ("GET", "HEAD") or (
            endpoints is not None and request.endpoint not in endpoints
        ):
            return None
        try:
            consumo = consumir_limite(identificar_ip(request))
        except IdentidadeIPInvalida:
            return erro_json("IP_NAO_IDENTIFICADO",
                             "Não foi possível identificar o IP da conexão ou do proxy.", 400)
        except SQLAlchemyError as erro:
            current_app.logger.error("Limitador de %s indisponível (%s)", recurso, type(erro).__name__)
            resposta, status = erro_json(
                "LIMITE_INDISPONIVEL",
                "Não foi possível verificar o limite. Tente novamente em instantes.", 503)
            return resposta, status, {"Retry-After": "5", "Cache-Control": "no-store"}
        g.limite_consulta = consumo
        if not consumo.permitido:
            resposta, status = erro_json("LIMITE_EXCEDIDO", mensagem_excedido, 429,
                                         tentar_em_segundos=consumo.tentar_em)
            return resposta, status, {"Retry-After": str(consumo.tentar_em),
                                      "Cache-Control": "no-store"}
        return None

    @blueprint.after_request
    def _informar(resposta):
        consumo = g.get("limite_consulta")
        if consumo is not None:
            resposta.headers.update(consumo.cabecalhos())
            resposta.headers["Cache-Control"] = "no-store"
        return resposta
