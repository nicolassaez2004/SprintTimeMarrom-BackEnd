"""Mede a defasagem coleta → tela do mapa ao vivo (US22), só com GETs HTTP.

Repete o comportamento do frontend: consulta /posicoes/atuais a cada
`--intervalo` segundos com If-None-Match. A cada segundo calcula a idade da
coleta mais nova exibida (agora − maior `coletado_em` recebido). Não acessa o
banco nem a SPTrans.

    python -m scripts.medir_defasagem_mapa --api http://127.0.0.1:5000 --duracao 300
"""

import argparse
import json
import time
from datetime import datetime, timezone

import requests

LIMITE_SEGUNDOS = 60


def medir(api, duracao, intervalo, sessao=None, relogio=time.time, dormir=time.sleep):
    sessao = sessao or requests.Session()
    url = api.rstrip("/") + "/posicoes/atuais"
    etag, exibida, ciclos, idades = None, None, [], []
    respostas = {"200": 0, "304": 0, "erro": 0}
    inicio = relogio()
    proxima = inicio
    while (agora := relogio()) < inicio + duracao:
        if agora >= proxima:
            proxima += intervalo
            try:
                r = sessao.get(url, headers={"If-None-Match": etag} if etag else {}, timeout=30)
                if r.status_code == 304:
                    respostas["304"] += 1
                elif r.ok:
                    respostas["200"] += 1
                    etag = r.headers.get("ETag")
                    datas = [v["coletado_em"] for v in r.json()["veiculos"] if v.get("coletado_em")]
                    if datas:
                        nova = max(datetime.fromisoformat(d).timestamp() for d in datas)
                        if exibida is None or nova > exibida:
                            ciclos.append(round(relogio() - nova, 2))
                            exibida = nova
                else:
                    respostas["erro"] += 1
            except (requests.RequestException, ValueError, KeyError):
                respostas["erro"] += 1
        if exibida is not None:
            idades.append(relogio() - exibida)
        dormir(1)
    idades.sort()
    return {
        "medido_em": datetime.now(timezone.utc).isoformat(),
        "api": api,
        "duracao_segundos": duracao,
        "intervalo_consulta_segundos": intervalo,
        "respostas": respostas,
        "ciclos_exibidos": len(ciclos),
        "coleta_ate_tela_por_ciclo_s": ciclos,
        "defasagem_exibida_max_s": round(idades[-1], 2) if idades else None,
        "defasagem_exibida_p95_s": round(idades[int(len(idades) * 0.95) - 1], 2) if idades else None,
        "limite_s": LIMITE_SEGUNDOS,
        "dentro_do_limite": bool(idades) and idades[-1] <= LIMITE_SEGUNDOS,
        "nota": "Idade da coleta mais nova exibida. Veículos cujo GPS a SPTrans não "
                "atualizou mantêm o coletado_em da primeira gravação e aparecem como antigos.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--api", default="http://127.0.0.1:5000")
    parser.add_argument("--duracao", type=int, default=300)
    parser.add_argument("--intervalo", type=int, default=10)
    parser.add_argument("--saida")
    args = parser.parse_args()
    resultado = medir(args.api, args.duracao, args.intervalo)
    texto = json.dumps(resultado, ensure_ascii=False, indent=2)
    if args.saida:
        with open(args.saida, "w", encoding="utf-8") as arquivo:
            arquivo.write(texto + "\n")
    print(texto)
    return 0 if resultado["dentro_do_limite"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
