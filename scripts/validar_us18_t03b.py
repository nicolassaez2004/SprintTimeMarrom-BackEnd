"""US18-T03B: confere os vinte casos CONTROLADOS de espera e excesso pela API real.

Reaproveita a montagem do ensaio da US17-T03B (rede A→B→C, coleta por minuto,
viagens com passagem em B e registro da estimativa) e acrescenta a programação
OFICIAL por faixas (``[início, fim, H]``), da qual saem os intervalos programados
vigentes. As viagens ficam numa captura GTFS de apoio, que a US04 não escolhe.
Chama ``calcular_e_publicar_espera`` (US18-T02) e ``GET /indicadores?tipo=ESPERA``
(US18-T03A). SQLite em memória; nunca o banco configurado.

Executar: ``python -m scripts.validar_us18_t03b``.
"""

import argparse
from collections import Counter
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import subprocess

from sqlalchemy import event

from app import create_app, db
from app.indicadores.espera import calcular_e_publicar_espera
from app.models import ImportacaoProgramacao, PartidaProgramada
from scripts.validar_us17_t03b import (
    AGORA, LACUNA, LINHA, MATCH, RECON, UTC, _passagem, _preparar, _viagem, exigir_sqlite_em_memoria,
    hash_texto, instante,
)

RAIZ = Path(__file__).resolve().parents[1]
CADERNO = RAIZ / "docs/validacoes/US18-T03B-casos.json"


def _dias(caderno, caso):
    return [{**caderno["padrao"], **d} for d in caso["dias"]]


def _semear(caso, dias, versao, coleta, uid):
    importacoes, seq = {}, 0
    for dia in dias:
        data = date.fromisoformat(dia["data"])
        oficial = ImportacaoProgramacao(
            id=uid(f"oficial:{dia['data']}"), versao_gtfs_id=versao.id, linha_codigo=LINHA, data_operacao=data,
            sentido="0", tipo_dia="UTIL", fonte="OFICIAL", fonte_url="urn:us18-t03b:programacao",
            versao_fonte=f"programacao-{dia['data']}", capturada_em=AGORA, verificada_em=AGORA, arquivo_id=uid("arquivo"))
        db.session.add(oficial)
        ordem = 0
        for inicio, fim, intervalo in dia["programacao"]:
            minuto = inicio
            while minuto < fim:
                ordem += 1
                frequencia = dia["oferta"] == "FREQUENCIA"
                db.session.add(PartidaProgramada(
                    id=uid(f"programada:{dia['data']}:{ordem}"), importacao=oficial, ordem=ordem,
                    saida_prevista=instante(dia["data"], minuto), tipo_oferta=dia["oferta"],
                    intervalo_seg=intervalo if frequencia else None))
                minuto += intervalo / 60
        for sentido in ("0", "1"):  # captura de apoio só para hospedar as viagens
            importacoes[dia["data"], sentido] = ImportacaoProgramacao(
                id=uid(f"apoio:{dia['data']}:{sentido}"), versao_gtfs_id=versao.id, linha_codigo=LINHA,
                data_operacao=data, sentido=sentido, tipo_dia="UTIL", fonte="GTFS", fonte_url="urn:us18-t03b:apoio",
                versao_fonte=f"apoio-{dia['data']}-{sentido}", capturada_em=AGORA, verificada_em=AGORA,
                arquivo_id=uid("arquivo"))
            db.session.add(importacoes[dia["data"], sentido])
        db.session.flush()
        grupos = [(s, m, "FREQUENCIA") for s, ms in dia.get("passagens", {}).items() for m in ms]
        grupos += [(s, m, "FIXA") for s, ms in dia.get("passagens_fixas", {}).items() for m in ms]
        for sentido, minuto, tipo in grupos:
            seq += 1
            viagem, t = _viagem(dia, sentido, minuto, tipo, versao, coleta, importacoes, uid, seq)
            _passagem(viagem, t, versao, coleta, seq)
        for a, b in dia.get("sem_evidencia", ()):
            seq += 1
            _viagem(dia, "0", a, "FREQUENCIA", versao, coleta, importacoes, uid, seq, sem_evidencia=[
                {"parada": "B", "inicio": instante(dia["data"], a).isoformat(),
                 "fim": instante(dia["data"], b).isoformat()}])
    db.session.commit()


def _consulta(caso, dias):
    c = caso["consulta"]
    params = {"tipo": "ESPERA", "linha": LINHA, "sentido": "0", "parada": "B"}
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
                    calcular_e_publicar_espera(
                        linha=LINHA, sentido="0", data_operacao=date.fromisoformat(dia["data"]),
                        versao_gtfs_id=versao.id, versao_correspondencia=MATCH, versao_reconstrucao=RECON,
                        agora=AGORA, intervalo=60, lacuna_maxima=LACUNA)
            parametros = _consulta(caso, dias)
            resposta = app.test_client().get("/indicadores", query_string=parametros)
            corpo = resposta.get_json()
            itens = corpo.get("dados", [])
            e, tol = caso["esperado"], caderno["tolerancia_min"]
            comparacoes = []

            def comparar(campo, manual, obtido, numerico=False):
                confere = (obtido is not None and abs(manual - obtido) <= tol
                           if numerico and manual is not None else manual == obtido)
                comparacoes.append({"campo": campo, "manual": manual, "obtido": obtido, "confere": confere})

            comparar("http_status", 200, resposta.status_code)
            comparar("quantidade_itens", 1, len(itens))
            item = itens[0] if itens else {}
            espera, excesso = item.get("espera_estimada") or {}, item.get("excesso_espera") or {}
            comparar("espera_min", e["espera"], espera.get("valor"), numerico=True)
            comparar("excesso_min", e["excesso"], excesso.get("valor"), numerico=True)
            comparar("motivo_espera", e["motivo_espera"], espera.get("motivo_ausencia"))
            comparar("motivo_excesso", e["motivo_excesso"], excesso.get("motivo_ausencia"))
            comparar("unidades", ["min", "min"], [espera.get("unidade"), excesso.get("unidade")])
            comparar("parada", "B", item.get("parada"))
            comparar("ausencia_nao_e_zero", [e["espera"] is None, e["excesso"] is None],
                     [espera.get("valor") is None, excesso.get("valor") is None])
            return {"id": caso["id"], "titulo": caso["titulo"], "escala": caso["escala"],
                    "status": "CONFERIU_EXEMPLO" if all(c["confere"] for c in comparacoes) else "DIVERGENTE",
                    "calculo_manual": caso["manual"], "consulta": parametros, "comparacoes": comparacoes,
                    "resposta_api": corpo}
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
    return {"tarefa": "US18-T03B", "origem": caderno["origem"], "executado_em": datetime.now(UTC).isoformat(),
            "aceite_institucional": caderno["aceite_institucional"],
            # O critério comum US14–US19 exige os 20 casos aprovados pela Instituição Parceira:
            # a conferência técnica sozinha não deixa a tarefa pronta (review 25/11/2026).
            "envio_instituicao_parceira": caderno.get("envio_instituicao_parceira", "NAO_ENVIADO"),
            "conferencia_tecnica_ok": all(r["status"] == "CONFERIU_EXEMPLO" for r in resultados),
            "pronto": (all(r["status"] == "CONFERIU_EXEMPLO" for r in resultados)
                       and caderno["aceite_institucional"] == "APROVADO"),
            "fonte_casos": "docs/validacoes/US18-T03B-casos.json", "sha256_caderno": hash_texto(CADERNO),
            "codigo_base": commit.stdout.strip() if commit.returncode == 0 else None,
            "banco": "SQLite em memória; FK ON; sem Aiven", "relogio_ensaio": AGORA.isoformat(),
            "resumo": dict(Counter(r["status"] for r in resultados)), "casos": resultados}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--saida", type=Path, default=RAIZ / "docs/evidencias/US18-T03B-conferencia.json")
    args = parser.parse_args()
    relatorio = executar()
    args.saida.write_text(json.dumps(relatorio, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"resumo": relatorio["resumo"], "aceite": relatorio["aceite_institucional"],
                      "conferencia_tecnica_ok": relatorio["conferencia_tecnica_ok"],
                      "pronto": relatorio["pronto"]}, ensure_ascii=False))
    return 0 if relatorio["conferencia_tecnica_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
