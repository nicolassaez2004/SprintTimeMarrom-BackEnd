"""Sondas HTTP a executar em processo e máquina separados do serviço monitorado."""
from datetime import datetime, timezone
import time
from urllib.parse import urlsplit

import requests

from app import db
from app.models import VerificacaoDisponibilidade


def verificar(alvo, url, *, texto_esperado=None, timeout=5, sessao=requests,
              relogio=time.monotonic, agora=None):
    if alvo not in ("API", "PAINEL"):
        raise ValueError("alvo inválido")
    partes = urlsplit(url)
    if partes.scheme not in ("http", "https") or not partes.hostname or partes.username or partes.password:
        raise ValueError("URL deve ser HTTP(S) e não conter credenciais")
    if not 0 < timeout <= 15:
        raise ValueError("timeout deve estar entre 0 e 15 segundos")
    instante = (agora or (lambda: datetime.now(timezone.utc)))()
    inicio = relogio()
    status = None
    try:
        resposta = sessao.get(url, timeout=timeout, allow_redirects=False, stream=True)
        try:
            status = resposta.status_code
            if not 200 <= status < 300:
                resultado = "HTTP_ERRO"
            elif texto_esperado is not None:
                # Limite para que uma resposta enorme não seja carregada inteira.
                conteudo = b""
                for bloco in resposta.iter_content(chunk_size=8192):
                    conteudo += bloco
                    if len(conteudo) >= 262144:
                        break
                resultado = "OK" if texto_esperado.encode("utf-8") in conteudo else "CONTEUDO_INESPERADO"
            else:
                resultado = "OK"
        finally:
            resposta.close()
    except requests.Timeout:
        resultado = "TIMEOUT"
    except requests.RequestException:
        resultado = "ERRO_CONEXAO"
    duracao = max(0, round((relogio() - inicio) * 1000))
    return VerificacaoDisponibilidade(alvo=alvo, instante=instante, duracao_ms=duracao,
                                      sucesso=resultado == "OK", status_http=status, resultado=resultado)


def sondar_e_gravar(url_api, url_painel, *, texto_api=None, texto_painel=None, timeout=5):
    """Mede ambos os alvos e confirma os dois registros na mesma transação."""
    medicoes = [verificar("API", url_api, texto_esperado=texto_api, timeout=timeout),
                verificar("PAINEL", url_painel, texto_esperado=texto_painel, timeout=timeout)]
    db.session.add_all(medicoes)
    db.session.commit()
    return medicoes
