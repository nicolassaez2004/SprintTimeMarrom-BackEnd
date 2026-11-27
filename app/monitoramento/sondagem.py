"""US30-T01B: sondas HTTP da API e do painel, fora do serviço monitorado.

Executadas por ``scripts/sondagem.py`` (processo próprio, idealmente noutra
máquina). Nunca rodam dentro de uma requisição Flask.

O que cada sonda comprova (``alvo`` grava esse rótulo, sem host nem query):

- API: ``GET`` na URL configurada respondeu 2xx dentro do timeout e, se pedido,
  o corpo contém o texto esperado. Usar ``/saude`` testa Flask + consulta ao
  banco; ``/health`` só prova que o processo Flask responde, não o banco.
- PAINEL: o servidor do frontend entregou a página com o texto esperado. Não
  executa JavaScript nem prova que gráficos, mapa ou chamadas à API funcionaram.

Resposta lenta dentro do timeout é ``OK`` com ``latencia_ms``; acima dele é
``TIMEOUT``. Falha de um alvo não impede medir/gravar o outro.
"""
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urlsplit

import requests

from app import db
from app.models import VerificacaoSaude

logger = logging.getLogger(__name__)
COMPONENTES = ("API", "PAINEL")
LIMITE_CORPO = 262144


def _alvo(url):
    partes = urlsplit(url)
    if partes.scheme not in ("http", "https") or not partes.hostname or partes.username or partes.password:
        raise ValueError("URL deve ser HTTP(S) e não conter credenciais")
    return f"GET {partes.path or '/'}"[:64]


def verificar(componente, url, *, texto_esperado=None, timeout=5, sessao=requests,
              relogio=time.monotonic, agora=None):
    """Mede um alvo e devolve ``VerificacaoSaude`` (não grava)."""
    if componente not in COMPONENTES:
        raise ValueError("componente inválido")
    alvo = _alvo(url)
    if not 0 < timeout <= 15:
        raise ValueError("timeout deve estar entre 0 e 15 segundos")
    instante = (agora or (lambda: datetime.now(timezone.utc)))()
    inicio, status = relogio(), None
    try:
        resposta = sessao.get(url, timeout=timeout, allow_redirects=False, stream=True)
        try:
            status = resposta.status_code
            if not 200 <= status < 300:
                detalhe = "HTTP_ERRO"
            elif texto_esperado is not None:
                conteudo = b""
                for bloco in resposta.iter_content(chunk_size=8192):
                    conteudo += bloco
                    if len(conteudo) >= LIMITE_CORPO:
                        break
                detalhe = "OK" if texto_esperado.encode("utf-8") in conteudo else "CONTEUDO_INESPERADO"
            else:
                detalhe = "OK"
        finally:
            resposta.close()
    except requests.Timeout:
        detalhe = "TIMEOUT"
    except requests.RequestException:
        detalhe = "ERRO_CONEXAO"
    latencia = max(0, round((relogio() - inicio) * 1000))
    return VerificacaoSaude(componente=componente, verificada_em=instante, latencia_ms=latencia,
                            estado="OK" if detalhe == "OK" else "INDISPONIVEL", status_http=status,
                            detalhe=detalhe, alvo=alvo)


def sondar_e_gravar(alvos, *, timeout=5, verificador=verificar):
    """Mede os alvos em paralelo e grava cada um em transação própria.

    ``alvos``: {componente: (url, texto_esperado)}. Retorna {componente: medição
    ou None}; None = a medição não foi gravada (aparecerá como lacuna).
    """
    with ThreadPoolExecutor(max_workers=len(alvos) or 1) as pool:
        futuros = {c: pool.submit(verificador, c, url, texto_esperado=texto, timeout=timeout)
                   for c, (url, texto) in alvos.items()}
    gravadas = {}
    for componente, futuro in futuros.items():
        try:
            medicao = futuro.result()
            db.session.add(medicao)
            db.session.commit()
            gravadas[componente] = medicao
        except Exception as erro:  # um alvo nunca derruba o outro
            db.session.rollback()
            gravadas[componente] = None
            logger.error('{"evento":"sondagem_nao_gravada","componente":"%s","erro":"%s"}',
                         componente, type(erro).__name__)
    return gravadas
