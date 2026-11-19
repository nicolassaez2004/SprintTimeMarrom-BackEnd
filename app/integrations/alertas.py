"""Canal externo dos alertas da coleta (webhook Discord/Slack/Teams).

A URL do webhook é segredo: nenhuma mensagem de erro a inclui.
"""

import requests

TIMEOUT_SEGUNDOS = 5


class AlertaEntregaError(RuntimeError):
    """O canal não confirmou o recebimento (rede ou HTTP fora de 2xx)."""


class AlertaWebhook:
    canal = "WEBHOOK"

    def __init__(self, url, session=None):
        self.url = url
        self.session = session or requests.Session()

    def __call__(self, evento):
        texto = (
            f"[Coleta Olho Vivo] {evento['mensagem']} "
            f"Início: {evento.get('inicio') or '-'}; "
            f"fim: {evento.get('fim') or 'em aberto'}; "
            f"causa: {evento.get('causa') or '-'}."
        )
        try:
            # `content` é lido pelo Discord; `text`, pelo Slack/Teams.
            resposta = self.session.post(
                self.url,
                json={"content": texto, "text": texto},
                timeout=TIMEOUT_SEGUNDOS,
            )
        except requests.RequestException as erro:
            raise AlertaEntregaError(type(erro).__name__) from None
        if not 200 <= resposta.status_code < 300:
            raise AlertaEntregaError(f"HTTP {resposta.status_code}")
