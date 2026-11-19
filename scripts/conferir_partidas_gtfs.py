"""US04-T04: confere 30 linhas reais em SQLite local, sem acessar a Aiven.

O total de referência é calculado diretamente dos CSVs do ZIP, sem reutilizar
o parser/calendário/gerador da aplicação. A observação vem do GET /partidas
depois da geração e persistência reais da US04-T02B.
"""

import argparse
import csv
import hashlib
import io
import json
import shutil
import zipfile
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


class ReferenciaGTFS:
    """Contagem independente; a expansão das faixas usa divisão inteira."""

    def __init__(self, caminho):
        self.caminho = Path(caminho)
        self.sha256 = hashlib.sha256(self.caminho.read_bytes()).hexdigest()
        with zipfile.ZipFile(self.caminho) as arquivo:
            def ler(nome):
                nomes = [n for n in arquivo.namelist() if Path(n).name == nome]
                if not nomes:
                    return []
                if len(nomes) != 1:
                    raise ValueError(f"{nome}: arquivo ambíguo")
                with arquivo.open(nomes[0]) as entrada:
                    return list(enumerate(csv.DictReader(io.TextIOWrapper(entrada, encoding="utf-8-sig")), 2))

            self.rotas = {r["route_id"]: r for _, r in ler("routes.txt")}
            self.calendario = ler("calendar.txt")
            self.excecoes = ler("calendar_dates.txt")
            self.viagens = ler("trips.txt")
            self.faixas = defaultdict(list)
            for numero, faixa in ler("frequencies.txt"):
                self.faixas[faixa["trip_id"]].append((numero, faixa))
            self.primeiras = {}
            for numero, parada in ler("stop_times.txt"):
                viagem = parada["trip_id"]
                ordem = int(parada["stop_sequence"])
                anterior = self.primeiras.get(viagem)
                if anterior is None or ordem < anterior[0]:
                    self.primeiras[viagem] = (ordem, numero, parada)
        if not self.rotas or not self.viagens or not (self.calendario or self.excecoes):
            raise ValueError("GTFS não contém a referência mínima da conferência")

    def ativos(self, dia):
        coluna = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")[dia.weekday()]
        data_gtfs = dia.strftime("%Y%m%d")
        ativos = {r["service_id"] for _, r in self.calendario
                  if r["start_date"] <= data_gtfs <= r["end_date"] and r[coluna] == "1"}
        for _, r in self.excecoes:
            if r["date"] == data_gtfs:
                if r["exception_type"] == "1":
                    ativos.add(r["service_id"])
                elif r["exception_type"] == "2":
                    ativos.discard(r["service_id"])
                else:
                    raise ValueError("exception_type inválido na referência")
        return ativos

    @staticmethod
    def segundos(valor):
        h, m, s = map(int, valor.split(":"))
        if h < 0 or not 0 <= m < 60 or not 0 <= s < 60:
            raise ValueError("Horário inválido na referência")
        return 3600 * h + 60 * m + s

    def contar(self, linha, sentido, dia):
        if linha not in self.rotas:
            raise ValueError("Linha ausente da referência")
        ativos = self.ativos(dia)
        totais = {"FIXA": 0, "FREQUENCIA": 0}
        referencias = []
        for numero, viagem in self.viagens:
            if (viagem["route_id"] != linha or viagem["direction_id"] != sentido
                    or viagem["service_id"] not in ativos):
                continue
            trip = viagem["trip_id"]
            if trip not in self.primeiras:
                raise ValueError("Viagem ativa sem stop_times na referência")
            _, primeira_linha, primeira = self.primeiras[trip]
            self.segundos(primeira.get("departure_time") or primeira.get("arrival_time"))
            faixas = self.faixas.get(trip, [])
            total = 0
            if faixas:
                for _, faixa in faixas:
                    inicio = self.segundos(faixa["start_time"])
                    fim = self.segundos(faixa["end_time"])
                    intervalo = int(faixa["headway_secs"])
                    if intervalo <= 0 or fim <= inicio:
                        raise ValueError("Faixa inválida na referência")
                    # Quantidade de instantes start + n*headway estritamente menores que end.
                    total += (fim - inicio + intervalo - 1) // intervalo
                tipo = "FREQUENCIA"
            else:
                total, tipo = 1, "FIXA"
            totais[tipo] += total
            referencias.append({"trip_id": trip, "service_id": viagem["service_id"],
                "trips_linha": numero, "primeiro_stop_times_linha": primeira_linha,
                "frequencies_linhas": [n for n, _ in faixas], "tipo_oferta": tipo, "total": total})
        return {"total": sum(totais.values()), "por_tipo": totais, "referencias": referencias}


def montar_amostra(referencia, data_base):
    util = data_base
    while util.weekday() >= 5:
        util += timedelta(days=1)
    sabado = util + timedelta(days=5 - util.weekday())
    dias = [(util, "UTIL"), (sabado, "SABADO"), (sabado + timedelta(days=1), "DOMINGO")]
    codigos = sorted(referencia.rotas)
    escolhidos = set()
    amostra = []
    if len(codigos) < 30:
        raise ValueError("Referência tem menos de 30 linhas distintas")
    for indice in range(30):
        dia, tipo = dias[indice % 3]
        inicio = indice * (len(codigos) - 1) // 29
        for deslocamento in range(len(codigos)):
            codigo = codigos[(inicio + deslocamento) % len(codigos)]
            if codigo in escolhidos:
                continue
            sentidos = {s: referencia.contar(codigo, s, dia) for s in ("0", "1")}
            if sum(r["total"] for r in sentidos.values()) == 0:
                continue
            escolhidos.add(codigo)
            amostra.append({"linha": codigo, "data": dia.isoformat(), "tipo_dia": tipo,
                            "sentidos": sentidos})
            break
        else:
            raise ValueError("Não foi possível selecionar 30 linhas com oferta nas datas")
    return amostra


def comparar(esperado, http_status, resposta, hash_fonte):
    """Ausência/erro nunca é convertida em zero, e divergência nunca é aprovada."""
    diferencas = []
    if http_status != 200:
        return {"estado": "ERRO_CONSULTA", "aprovado": False, "total_api": None,
                "diferenca": None, "diferencas": [f"HTTP_{http_status}"]}
    partidas = resposta.get("partidas")
    if not isinstance(partidas, list):
        return {"estado": "ERRO_CONSULTA", "aprovado": False, "total_api": None,
                "diferenca": None, "diferencas": ["LISTA_AUSENTE"]}
    total_api = resposta.get("quantidade")
    por_tipo = Counter(p.get("tipo_oferta") for p in partidas)
    if type(total_api) is not int or total_api != len(partidas) or total_api != esperado["total"]:
        diferencas.append("TOTAL_DIVERGENTE")
    if dict(por_tipo) != {k: v for k, v in esperado["por_tipo"].items() if v}:
        diferencas.append("TIPOS_DIVERGENTES")
    procedencia = resposta.get("procedencia") or {}
    if procedencia.get("fonte") != "GTFS" or procedencia.get("versao_fonte") != hash_fonte:
        diferencas.append("PROCEDENCIA_DIVERGENTE")
    if resposta.get("estado") not in ("DISPONIVEL", "SEM_PARTIDAS"):
        diferencas.append("GRADE_NAO_PERSISTIDA")
    return {"estado": "CONFERE_LOCALMENTE" if not diferencas else "DIVERGENTE",
            "aprovado": False, "total_api": total_api, "por_tipo_api": dict(por_tipo),
            "diferenca": total_api - esperado["total"] if type(total_api) is int else None,
            "diferencas": diferencas}


def executar(caminho_gtfs, data_base, diretorio_saida, fonte_url):
    # Imports da aplicação separados do cálculo independente acima.
    from app import create_app, db
    from app.models import ArquivoArmazenado, Linha, VersaoGTFS
    from app.programacao.service import gerar_programacao_gtfs

    referencia = ReferenciaGTFS(caminho_gtfs)
    amostra = montar_amostra(referencia, data_base)
    saida = Path(diretorio_saida).resolve()
    saida.mkdir(parents=True, exist_ok=False)  # Não sobrescrever evidências anteriores.
    armazenamento = saida / "gtfs"
    armazenamento.mkdir()
    consultas = saida / "consultas"
    consultas.mkdir()
    chave = f"gtfs/{referencia.sha256}.zip"
    shutil.copyfile(caminho_gtfs, armazenamento / f"{referencia.sha256}.zip")
    agora = datetime.now(timezone.utc)
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///" + (saida / "conferencia.sqlite").as_posix(),
                      "MYSQL_SSL_CA": None, "GTFS_STORAGE_DIR": str(armazenamento)})
    resultados = []
    with app.app_context():
        db.create_all()  # Apenas SQLite novo, explicitamente selecionado acima.
        arquivo = ArquivoArmazenado(chave=chave, nome_original="gtfs.zip", mime_type="application/zip",
            tamanho_bytes=Path(caminho_gtfs).stat().st_size, sha256=referencia.sha256, criado_em=agora)
        datas = [date.fromisoformat(r["data"]) for r in amostra]
        # Vigência local limitada aos dias da amostra; não altera a versão na Aiven.
        versao = VersaoGTFS(hash=referencia.sha256, capturada_em=agora,
                            vigencia_inicio=min(datas), vigencia_fim=max(datas), arquivo=arquivo)
        db.session.add(versao)
        db.session.flush()
        for caso in amostra:
            rota = referencia.rotas[caso["linha"]]
            db.session.add(Linha(versao_gtfs_id=versao.id, codigo=caso["linha"],
                                nome=rota.get("route_long_name") or caso["linha"]))
        db.session.commit()
        cliente = app.test_client()
        versao_local = versao.id
        for indice, caso in enumerate(amostra, 1):
            verificacoes = {}
            for sentido, esperado in caso["sentidos"].items():
                try:
                    gerar_programacao_gtfs(caso["linha"], sentido, date.fromisoformat(caso["data"]),
                                           caso["tipo_dia"], versao_gtfs_id=versao_local)
                    resposta = cliente.get("/partidas", query_string={"linha": caso["linha"],
                        "sentido": sentido, "data": caso["data"], "tipo_dia": caso["tipo_dia"],
                        "versao_gtfs_id": versao_local})
                    payload = resposta.get_json() or {}
                    nome = f"{indice:02d}-sentido-{sentido}.json"
                    (consultas / nome).write_bytes(resposta.data)
                    verificacao = comparar(esperado, resposta.status_code, payload, referencia.sha256)
                    verificacao.update(http_status=resposta.status_code, evidencia=f"consultas/{nome}",
                                        resposta_sha256=hashlib.sha256(resposta.data).hexdigest())
                except Exception as erro:
                    db.session.rollback()
                    verificacao = {"estado": "ERRO_GERACAO", "aprovado": False, "total_api": None,
                        "diferenca": None, "diferencas": [type(erro).__name__]}
                verificacoes[sentido] = {"referencia": esperado, "verificacao": verificacao}
            totais = [v["verificacao"]["total_api"] for v in verificacoes.values()]
            confere = all(v["verificacao"]["estado"] == "CONFERE_LOCALMENTE" for v in verificacoes.values())
            total_api = sum(totais) if all(type(v) is int for v in totais) else None
            total_fonte = sum(v["referencia"]["total"] for v in verificacoes.values())
            resultados.append({"caso": indice, "linha": caso["linha"], "data": caso["data"],
                "tipo_dia": caso["tipo_dia"], "sentidos": verificacoes, "total_fonte": total_fonte,
                "total_api": total_api, "diferenca": total_api - total_fonte if total_api is not None else None,
                "estado": "CONFERE_LOCALMENTE" if confere else "DIVERGENCIA_ABERTA", "aprovado": False})
            print(f"{indice:02d}/30 {caso['linha']} {caso['data']} fonte={total_fonte} api={total_api} {'CONFERE' if confere else 'ABERTO'}", flush=True)
        db.session.remove()
        db.engine.dispose()
    relatorio = {"tarefa": "US04-T04", "executada_em": agora.isoformat(), "ambiente": "SQLite local, feed real",
        "fonte": {"url": fonte_url, "sha256": referencia.sha256, "tamanho_bytes": Path(caminho_gtfs).stat().st_size},
        "versao_gtfs_local": versao_local, "linhas_distintas": len({r["linha"] for r in resultados}),
        "consultas": len(resultados) * 2, "conferem": sum(r["estado"] == "CONFERE_LOCALMENTE" for r in resultados),
        "divergencias_abertas": [r["caso"] for r in resultados if r["estado"] != "CONFERE_LOCALMENTE"],
        "aprovado": False, "revisao": "PENDENTE_OUTRA_PESSOA", "casos": resultados}
    (saida / "resultado.json").write_text(json.dumps(relatorio, ensure_ascii=False, indent=2), encoding="utf-8")
    return relatorio


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gtfs", required=True, type=Path)
    parser.add_argument("--data-base", required=True, type=date.fromisoformat)
    parser.add_argument("--saida", required=True, type=Path)
    parser.add_argument("--fonte-url", default="https://www.sptrans.com.br/umbraco/Surface/PerfilDesenvolvedor/BaixarGTFS?memberName=sptrans")
    args = parser.parse_args(argv)
    resultado = executar(args.gtfs, args.data_base, args.saida, args.fonte_url)
    print(f"Conferem: {resultado['conferem']}/30. Aprovação: pendente de revisão.")
    return 0 if resultado["conferem"] == 30 else 2


if __name__ == "__main__":
    raise SystemExit(main())
