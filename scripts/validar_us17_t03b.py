"""US17-T03B: confere os vinte casos CONTROLADOS de regularidade pela API real.

Cada caso cria e descarta um SQLite em memória (nunca o banco configurado):
rede A→B→C, programação da data, coleta por minuto (US13), viagens com uma
passagem estimada em B e o registro da estimativa (US15-T01). Em seguida chama
``calcular_e_publicar_regularidade`` (US17-T02) e ``GET /indicadores``
(US17-T03A), e compara com o cálculo manual do caderno.

Executar: ``python -m scripts.validar_us17_t03b``.
"""

import argparse
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import subprocess
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from sqlalchemy import event, insert

from app import create_app, db
from app.indicadores.regularidade import REGULARIDADE, calcular_e_publicar_regularidade
from app.models import (
    ArquivoArmazenado, EstimativaPassagemViagem, ExecucaoColeta, ImportacaoProgramacao, Linha, Parada,
    PartidaProgramada, PassagemEstimada, RegistroBrutoGPS, Trecho, VersaoGTFS, ViagemObservada,
)
from app.viagens.passagens import METODO, versao_estimativa

RAIZ = Path(__file__).resolve().parents[1]
CADERNO = RAIZ / "docs/validacoes/US17-T03B-casos.json"
FUSO = ZoneInfo("America/Sao_Paulo")
UTC = timezone.utc
LINHA, RECON, MATCH = "SINT-US17", "recon-controle-us17", "correspondencia-controlada-us17"
LACUNA = timedelta(seconds=120)
AGORA = datetime(2026, 12, 3, tzinfo=UTC)  # relógio do ENSAIO, após os cenários
PARADAS = {"A": -46.700, "B": -46.697, "C": -46.694}


def hash_texto(caminho):
    """UTF-8 com quebras LF, estável em checkouts Windows/Linux."""
    return sha256(Path(caminho).read_text(encoding="utf-8").encode("utf-8")).hexdigest()


def instante(dia, minutos):
    return (datetime.combine(date.fromisoformat(dia), time(), FUSO)
            + timedelta(seconds=round(minutos * 60))).astimezone(UTC)


def exigir_sqlite_em_memoria():
    if db.engine.dialect.name != "sqlite" or db.engine.url.database not in (None, "", ":memory:"):
        raise RuntimeError("O ensaio só pode usar SQLite em memória")


def _dias(caderno, caso):
    return [{**caderno["padrao"], **d} for d in caso["dias"]]


def _preparar(caso, dias):
    uid = lambda nome: str(uuid5(NAMESPACE_URL, f"us17-t03b:{caso['id']}:{nome}"))  # noqa: E731
    arquivo = ArquivoArmazenado(id=uid("arquivo"), chave="controle/rede-us17.json", nome_original="rede.json",
                                mime_type="application/json", tamanho_bytes=0, sha256="c" * 64, criado_em=AGORA)
    versao = VersaoGTFS(id=uid("rede"), hash="c" * 64, arquivo=arquivo, vigencia_inicio=date(2026, 1, 1),
                        vigencia_fim=date(2026, 12, 31))
    db.session.add_all([versao, Linha(versao_gtfs=versao, codigo=LINHA, nome="Linha de ensaio, sem operação real")])
    db.session.add_all(Parada(versao_gtfs_id=versao.id, codigo=c, nome=c, latitude=Decimal("-23.55"),
                              longitude=Decimal(str(lon))) for c, lon in PARADAS.items())
    for sentido, sequencia in (("0", "ABC"), ("1", "CBA")):
        db.session.add_all(Trecho(versao_gtfs_id=versao.id, linha_codigo=LINHA, sentido=sentido,
                                  trajeto=f"T{sentido}", ordem=i + 1, parada_origem_codigo=o, parada_destino_codigo=d,
                                  geometria=[[PARADAS[o], -23.55], [PARADAS[d], -23.55]])
                           for i, (o, d) in enumerate(zip(sequencia, sequencia[1:])))
    coleta = ExecucaoColeta(id=None, iniciada_em=AGORA, estado="OK", recebidos=1)
    db.session.add(coleta)
    db.session.flush()
    ciclos = set()
    for dia in dias:
        a, b = dia["coleta"]
        ciclos.update(instante(dia["data"], m) for m in range(a, b))
    db.session.execute(insert(ExecucaoColeta), [
        {"prevista_em": t, "iniciada_em": t, "estado": "OK", "recebidos": 1} for t in sorted(ciclos)])
    db.session.flush()
    return versao, coleta, uid


def _viagem(dia, sentido, minuto, tipo, versao, coleta, importacoes, uid, seq, *, estimar=True, sem_evidencia=()):
    """Partida da oferta pedida, viagem e (se houver) passagem estimada em B."""
    chave = (dia["data"], sentido)
    if chave not in importacoes:
        importacoes[chave] = ImportacaoProgramacao(
            id=uid(f"lote:{dia['data']}:{sentido}"), versao_gtfs_id=versao.id, linha_codigo=LINHA,
            data_operacao=date.fromisoformat(dia["data"]), sentido=sentido, tipo_dia="UTIL", fonte="GTFS",
            fonte_url="urn:us17-t03b", versao_fonte=f"controle-{dia['data']}-{sentido}", capturada_em=AGORA,
            verificada_em=AGORA, arquivo_id=uid("arquivo"))
        db.session.add(importacoes[chave])
    t = instante(dia["data"], minuto)
    partida = PartidaProgramada(id=uid(f"partida:{seq}"), importacao=importacoes[chave], ordem=seq,
                                saida_prevista=t - timedelta(minutes=5), tipo_oferta=tipo,
                                intervalo_seg=600 if tipo == "FREQUENCIA" else None)
    viagem = ViagemObservada(id=uid(f"viagem:{seq}"), partida_programada=partida,
                             saida_observada=t - timedelta(minutes=5), fim_observado=t + timedelta(minutes=5),
                             cobertura_tracado=1, versao_reconstrucao=RECON, versao_correspondencia=MATCH)
    db.session.add_all([partida, viagem])
    db.session.flush()  # a viagem existe antes do registro da estimativa (FK)
    if estimar:
        db.session.add(EstimativaPassagemViagem(
            viagem_observada_id=viagem.id, versao_estimativa=versao_estimativa(LACUNA),
            lacuna_maxima_seg=LACUNA.total_seconds(), executada_em=AGORA, sem_evidencia=list(sem_evidencia)))
    return viagem, t


def _passagem(viagem, t, versao, coleta, seq):
    brutos = [RegistroBrutoGPS(execucao_id=coleta.id, prefixo_origem=f"CONTROLE-{seq}", latitude=-23.55,
                               longitude=-46.697, horario_gps=t + timedelta(seconds=s), coletado_em=t,
                               payload={"origem": "SINTETICA_CONTROLADA"}) for s in (-10, 10)]
    db.session.add_all(brutos)
    db.session.flush()
    db.session.add(PassagemEstimada(viagem=viagem, versao_gtfs_id=versao.id, parada_codigo="B", instante=t,
                                    metodo=METODO, versao_estimativa=versao_estimativa(LACUNA),
                                    posicao_anterior_id=brutos[0].id, posicao_posterior_id=brutos[1].id))


def _semear(caso, dias, versao, coleta, uid):
    importacoes, seq = {}, 0
    for dia in dias:
        oferta = dia["oferta"]
        grupos = [(s, m, oferta) for s, ms in dia.get("passagens", {}).items() for m in ms]
        grupos += [(s, m, "FIXA") for s, ms in dia.get("passagens_fixas", {}).items() for m in ms]
        if not grupos:  # data sem passagens: a programação existe mesmo assim
            seq += 1
            _viagem(dia, "0", 300, oferta, versao, coleta, importacoes, uid, seq)
        for sentido, minuto, tipo in grupos:
            seq += 1
            viagem, t = _viagem(dia, sentido, minuto, tipo, versao, coleta, importacoes, uid, seq)
            db.session.flush()
            _passagem(viagem, t, versao, coleta, seq)
        for a, b in dia.get("sem_evidencia", ()):
            seq += 1
            _viagem(dia, "0", a, oferta, versao, coleta, importacoes, uid, seq, sem_evidencia=[
                {"parada": "B", "inicio": instante(dia["data"], a).isoformat(),
                 "fim": instante(dia["data"], b).isoformat()}])
        for minuto in dia.get("sem_estimativa", ()):
            seq += 1
            _viagem(dia, "0", minuto, oferta, versao, coleta, importacoes, uid, seq, estimar=False)
    db.session.commit()


def _consulta(caso, dias):
    c = caso["consulta"]
    params = {"tipo": REGULARIDADE, "linha": LINHA, "sentido": "0", "parada": "B"}
    if "janela" in c:
        t = instante(dias[0]["data"], c["janela"]).isoformat()
        return {**params, "inicio": t, "fim": t}
    params.update(inicio=instante(dias[0]["data"], 0).isoformat(), fim=instante(dias[-1]["data"], 1440).isoformat(),
                  agregacao="mes" if c.get("mes") else "dia" if c.get("dia") else "faixa")
    if c.get("faixa"):
        params.update(hora_inicio=c["faixa"][0], hora_fim=c["faixa"][1])
    return params


def conferir(caderno, caso):
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://", "MYSQL_SSL_CA": None,
                      "PASSAGEM_LACUNA_MAXIMA_SEG": str(int(LACUNA.total_seconds())),
                      "LIMITES_RELOGIO": lambda: 120, "INDICADORES_TRUSTED_PROXY_CIDRS": ""})
    with app.app_context():
        try:
            exigir_sqlite_em_memoria()
            event.listen(db.engine, "connect", lambda c, _: c.execute("PRAGMA foreign_keys=ON"))
            db.create_all()
            dias = _dias(caderno, caso)
            versao, coleta, uid = _preparar(caso, dias)
            _semear(caso, dias, versao, coleta, uid)
            for dia in dias:
                if not dia.get("sem_publicacao"):
                    calcular_e_publicar_regularidade(
                        linha=LINHA, sentido="0", data_operacao=date.fromisoformat(dia["data"]),
                        versao_gtfs_id=versao.id, versao_correspondencia=MATCH, versao_reconstrucao=RECON,
                        agora=AGORA, intervalo=60, lacuna_maxima=LACUNA)
            parametros = _consulta(caso, dias)
            resposta = app.test_client().get("/indicadores", query_string=parametros)
            corpo = resposta.get_json()
            itens = corpo.get("dados", [])
            e = caso["esperado"]
            comparacoes = []

            def comparar(campo, manual, obtido, tolerancia=None):
                confere = (obtido is not None and abs(manual - obtido) <= tolerancia
                           if tolerancia is not None and manual is not None else manual == obtido)
                comparacoes.append({"campo": campo, "manual": manual, "obtido": obtido, "confere": confere})

            comparar("http_status", 200, resposta.status_code)
            comparar("quantidade_itens", 1, len(itens))
            item = itens[0] if itens else {}
            comparar("cv", e["cv"], item.get("valor"), caderno["tolerancia_cv"])
            comparar("agrupamento", e["agrupamento"], item.get("agrupamento"))
            comparar("motivo_ausencia", e["motivo"], item.get("motivo_ausencia"))
            comparar("intervalos", e["intervalos"], item.get("intervalos"))
            comparar("unidade", "CV (adimensional)", item.get("unidade"))
            comparar("valor_nulo_nao_e_zero", e["cv"] is None, item.get("valor") is None)
            return {"id": caso["id"], "titulo": caso["titulo"], "escala": caso["escala"],
                    "status": "CONFERIU_EXEMPLO" if all(c["confere"] for c in comparacoes) else "DIVERGENTE",
                    "aceite_institucional": caderno["aceite_institucional"], "calculo_manual": caso["manual"],
                    "consulta": parametros, "comparacoes": comparacoes, "resposta_api": corpo}
        finally:
            db.session.remove()
            db.engine.dispose()


def executar(caderno=None):
    caderno = caderno or json.loads(CADERNO.read_text(encoding="utf-8"))
    assert [c["id"] for c in caderno["casos"]] == [f"C{i:02}" for i in range(1, 21)]
    assert Counter(c["escala"] for c in caderno["casos"]) == dict.fromkeys(("janela", "faixa", "dia", "mes"), 5)
    resultados = []
    for caso in caderno["casos"]:
        try:
            resultados.append(conferir(caderno, caso))
        except Exception as erro:
            resultados.append({"id": caso["id"], "titulo": caso["titulo"], "escala": caso["escala"],
                               "status": "FALHA_EXECUCAO", "erro": f"{type(erro).__name__}: {erro}"})
    commit = subprocess.run(["git", "-C", str(RAIZ), "rev-parse", "HEAD"], capture_output=True, text=True)
    return {"tarefa": "US17-T03B", "origem": caderno["origem"], "executado_em": datetime.now(UTC).isoformat(),
            "aceite_institucional": caderno["aceite_institucional"],
            # O critério comum US14–US19 exige os 20 casos aprovados pela Instituição Parceira:
            # a conferência técnica sozinha não deixa a tarefa pronta (review 25/11/2026).
            "envio_instituicao_parceira": caderno.get("envio_instituicao_parceira", "NAO_ENVIADO"),
            "conferencia_tecnica_ok": all(r["status"] == "CONFERIU_EXEMPLO" for r in resultados),
            "pronto": (all(r["status"] == "CONFERIU_EXEMPLO" for r in resultados)
                       and caderno["aceite_institucional"] == "APROVADO"),
            "fonte_casos": "docs/validacoes/US17-T03B-casos.json", "sha256_caderno": hash_texto(CADERNO),
            "codigo_base": commit.stdout.strip() if commit.returncode == 0 else None,
            "banco": "SQLite em memória; FK ON; sem Aiven", "relogio_ensaio": AGORA.isoformat(),
            "resumo": dict(Counter(r["status"] for r in resultados)), "casos": resultados}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--saida", type=Path, default=RAIZ / "docs/evidencias/US17-T03B-conferencia.json")
    args = parser.parse_args()
    relatorio = executar()
    args.saida.write_text(json.dumps(relatorio, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"resumo": relatorio["resumo"], "aceite": relatorio["aceite_institucional"],
                      "conferencia_tecnica_ok": relatorio["conferencia_tecnica_ok"],
                      "pronto": relatorio["pronto"]}, ensure_ascii=False))
    return 0 if relatorio["resumo"] == {"CONFERIU_EXEMPLO": 20} else 1


if __name__ == "__main__":
    raise SystemExit(main())
