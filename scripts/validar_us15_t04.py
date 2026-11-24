"""Confere o caderno CONTROLADO da US15-T04; nunca conecta ao banco configurado.

O êxito técnico não representa aceite do PO/Instituição Parceira. Todo cenário
cria e descarta seu próprio SQLite em memória, com chaves estrangeiras ativas.
"""

import argparse
from collections import Counter
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from fractions import Fraction
from hashlib import sha256
from importlib.metadata import version
import json
from pathlib import Path
import platform
import subprocess
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from pyproj import Transformer
from sqlalchemy import event, insert, select

from app import create_app, db
from app.indicadores.frequencia import OBSERVADA, calcular_frequencias
from app.models import (
    ArquivoArmazenado, ExecucaoColeta, ImportacaoProgramacao, IndicadorOperacional,
    Linha, ObservacaoTratada, Parada, PartidaProgramada, PassagemEstimada,
    RegistroBrutoGPS, Trecho, VersaoGTFS, ViagemObservada,
)
from app.viagens.passagens import estimar_e_gravar

RAIZ = Path(__file__).resolve().parents[1]
CADERNO = RAIZ / "docs/validacoes/US15-T04-casos.json"
FUSO = ZoneInfo("America/Sao_Paulo")
UTC = timezone.utc
LINHA = "SINT-US15"
RECON = "recon-controle-1"
MATCH = "correspondencia-controlada"
AGORA = datetime(2026, 12, 3, tzinfo=UTC)  # relógio do ENSAIO, após os cenários


def hash_texto(caminho):
    """Hash estável em checkouts Windows/Linux: UTF-8 com quebras de linha LF."""
    return sha256(Path(caminho).read_text(encoding="utf-8").encode("utf-8")).hexdigest()


def carregar_caderno(caminho=CADERNO):
    dados = json.loads(Path(caminho).read_text(encoding="utf-8"))
    assert dados["origem"] == "SINTETICA_CONTROLADA" and dados["aceite_institucional"] == "PENDENTE"
    assert [c["id"] for c in dados["casos"]] == [f"C{i:02}" for i in range(1, 21)]
    assert Counter(c["escala"] for c in dados["casos"]) == dict.fromkeys(("janela", "faixa", "dia", "mes"), 5)
    for caso in dados["casos"]:
        for item in caso["esperado"]:
            for lado in ("observada", "programada"):
                ref = item[lado]
                if ref["taxa"] is not None:
                    assert Fraction(ref["n"]) / Fraction(ref["horas"]) == Fraction(ref["taxa"]), caso["id"]
    return dados


def dias_do_caso(caderno, caso):
    dias = []
    for entrada in caso.get("dias", [{}]):
        base = {**deepcopy(caderno["dia_padrao"]), **deepcopy(entrada)}
        for deslocamento in range(base.get("repetir_dias", 1)):
            dia = {**base, "data": (date.fromisoformat(base["data"]) + timedelta(days=deslocamento)).isoformat()}
            dias.append(dia)
    return sorted(dias, key=lambda d: d["data"])


def instante(dia, minutos=0, segundos=0):
    return (datetime.combine(date.fromisoformat(dia), time(), FUSO)
            + timedelta(minutes=minutos, seconds=segundos)).astimezone(UTC)


def exigir_sqlite_em_memoria():
    """Verifica o destino antes de qualquer criação de tabela ou dado de ensaio."""
    if db.engine.dialect.name != "sqlite" or db.engine.url.database not in (None, "", ":memory:"):
        raise RuntimeError("O ensaio só pode usar SQLite em memória")


def _preparar(caderno, caso):
    """Rede métrica desenhada e horários do caderno, sem consultar fontes externas."""
    exigir_sqlite_em_memoria()
    uid = lambda nome: str(uuid5(NAMESPACE_URL, f"us15-t04:{caso['id']}:{nome}"))
    dias = dias_do_caso(caderno, caso)
    rede = caderno["rede_controlada"]
    inversa = Transformer.from_crs(rede["crs"], "EPSG:4326", always_xy=True)
    gps = lambda medida: inversa.transform(rede["x_origem"] + medida, rede["y"])
    conteudo = json.dumps(rede, sort_keys=True).encode()
    arquivo = ArquivoArmazenado(id=uid("arquivo"), chave="controle/rede-us15.json",
        nome_original="rede-controlada.json", mime_type="application/json", tamanho_bytes=len(conteudo),
        sha256=sha256(conteudo).hexdigest(), criado_em=AGORA)
    versao = VersaoGTFS(id=uid("rede"), hash=arquivo.sha256, arquivo=arquivo,
                        vigencia_inicio=date(2026, 1, 1), vigencia_fim=date(2026, 12, 31))
    linha = Linha(versao_gtfs=versao, codigo=LINHA, nome="Linha de ensaio, sem operação real")
    db.session.add_all([versao, linha])
    paradas = {}
    for codigo, medida in rede["paradas_m"].items():
        lon, lat = gps(medida)
        paradas[codigo] = Parada(versao_gtfs_id=versao.id, codigo=codigo,
                                  nome=f"Controle {codigo}", latitude=lat, longitude=lon)
        db.session.add(paradas[codigo])
    db.session.flush()
    db.session.refresh(paradas["A"])
    db.session.refresh(paradas["B"])
    db.session.refresh(paradas["C"])
    for sentido, sequencia in (("0", "ABC"), ("1", "CBA")):
        for ordem, (a, b) in enumerate(zip(sequencia, sequencia[1:]), 1):
            origem, destino = paradas[a], paradas[b]
            db.session.add(Trecho(id=uid(f"trecho:{sentido}:{ordem}"), versao_gtfs_id=versao.id,
                linha_codigo=LINHA, sentido=sentido, trajeto=f"controle-{sentido}", ordem=ordem,
                parada_origem_codigo=a, parada_destino_codigo=b,
                geometria=[[float(origem.longitude), float(origem.latitude)],
                           [float(destino.longitude), float(destino.latitude)]]))

    grades, contextos = {}, set()
    for dia in dias:
        coleta = dia["coleta"]
        if coleta:
            ciclos = [{"iniciada_em": instante(dia["data"], coleta["inicio"] + m),
                       "prevista_em": instante(dia["data"], coleta["inicio"] + m),
                       "fonte": "OLHO_VIVO", "estado": "OK", "recebidos": 0}
                      for m in range(coleta["minutos"])]
            # Interseções de dias operacionais podem compartilhar o mesmo ciclo civil.
            db.session.execute(insert(ExecucaoColeta), ciclos)
        g = dia["grade"]
        minutos = g.get("horarios") if "horarios" in g else [g["inicio"] + n * g["passo"] for n in range(g["n"])]
        for sentido in ("0", "1"):
            for tipo in (("UTIL", "FERIADO") if dia.get("ambigua") else ("UTIL",)):
                lote = ImportacaoProgramacao(id=uid(f"lote:{dia['data']}:{sentido}:{tipo}"),
                    versao_gtfs_id=versao.id, linha_codigo=LINHA, data_operacao=date.fromisoformat(dia["data"]),
                    sentido=sentido, tipo_dia=tipo, fonte="GTFS", fonte_url="urn:us15-t04:rede-controlada",
                    versao_fonte=sha256(json.dumps([dia["data"], sentido, tipo, minutos]).encode()).hexdigest(),
                    capturada_em=AGORA, verificada_em=AGORA, arquivo=arquivo)
                partidas = [PartidaProgramada(id=uid(f"partida:{lote.id}:{i}"), importacao=lote,
                    ordem=i + 1, saida_prevista=instante(dia["data"], m), tipo_oferta="FIXA") for i, m in enumerate(minutos)]
                db.session.add(lote)
                db.session.add_all(partidas)
                if tipo == "UTIL":
                    grades[dia["data"], sentido] = partidas
        contextos.add((dia["data"], caso["sentido"], RECON))
    db.session.commit()
    return {"dias": dias, "grades": grades, "versao": versao, "uid": uid,
            "gps": gps, "paradas": paradas, "contextos": contextos}


def _estimar(caderno, caso, contexto):
    referencias, usados, originais = [], set(), []
    execucao = db.session.query(ExecucaoColeta).first()
    sequencia = 0
    for dia in contexto["dias"]:
        for grupo in dia["viagens"]:
            sentido, recon = grupo.get("sentido", caso["sentido"]), grupo.get("recon", RECON)
            contexto["contextos"].add((dia["data"], sentido, recon))
            for repeticao in range(grupo["n"]):
                sequencia += 1
                t0 = instante(dia["data"], grupo["inicio"] + repeticao * grupo["passo"])
                candidatas = [p for p in contexto["grades"][dia["data"], sentido] if (p.id, recon) not in usados]
                partida = min(candidatas, key=lambda p: abs((p.saida_prevista - t0).total_seconds()))
                usados.add((partida.id, recon))
                viagem = ViagemObservada(id=contexto["uid"](f"viagem:{sequencia}"), partida_programada=partida,
                    saida_observada=t0, fim_observado=t0 + timedelta(seconds=max(p[0] for p in grupo["gps"])),
                    cobertura_tracado=1, versao_reconstrucao=recon, versao_correspondencia=MATCH)
                db.session.add(viagem)
                observacoes, entrada = [], []
                for segundos, medida, valida in grupo["gps"]:
                    codigo = next((c for c, m in caderno["rede_controlada"]["paradas_m"].items() if m == medida), None)
                    if codigo:
                        p = contexto["paradas"][codigo]
                        lon, lat = float(p.longitude), float(p.latitude)
                    else:
                        lon, lat = contexto["gps"](medida)
                    horario = t0 + timedelta(seconds=segundos)
                    bruto = RegistroBrutoGPS(execucao=execucao, prefixo_origem=f"CONTROLE-{sequencia}",
                        latitude=lat, longitude=lon, horario_gps=horario, coletado_em=horario,
                        payload={"origem": "SINTETICA_CONTROLADA", "caso": caso["id"], "medida_m": medida})
                    db.session.add(bruto)
                    obs = ObservacaoTratada(bruto=bruto, valida=valida, versao_gtfs_id=contexto["versao"].id,
                        linha_codigo=LINHA, sentido_gtfs=sentido, versao_correspondencia=MATCH)
                    db.session.add_all([bruto, obs])
                    observacoes.append(obs)
                    entrada.append({"instante": horario.isoformat(), "medida_m": medida,
                                    "longitude": lon, "latitude": lat, "valida": valida})
                db.session.flush()
                originais.extend((o.bruto_id, o.bruto.horario_gps, o.bruto.latitude, o.bruto.longitude)
                                 for o in observacoes)
                resultado = estimar_e_gravar(viagem, observacoes,
                    lacuna_maxima=timedelta(seconds=caderno["lacuna_maxima_segundos_somente_ensaio"]))
                alvo = next((p for p in resultado.passagens if p.parada_codigo == caso["parada"]), None)
                esperado = None if grupo["passagem_alvo_seg"] is None else t0 + timedelta(seconds=grupo["passagem_alvo_seg"])
                diferenca = None if esperado is None or alvo is None else (alvo.instante - esperado).total_seconds()
                rejeicoes = sorted({r[2] for r in resultado.rejeicoes})
                confere = ((alvo is None) if esperado is None else diferenca is not None and
                           abs(diferenca) <= caderno["tolerancia_interpolacao_segundos"])
                if grupo.get("rejeicao"):
                    confere = confere and grupo["rejeicao"] in rejeicoes
                referencias.append({"sentido": sentido, "reconstrucao": recon, "gps": entrada,
                    "horario_manual": esperado.isoformat() if esperado else None,
                    "horario_estimado": alvo.instante.isoformat() if alvo else None,
                    "diferenca_segundos": diferenca, "rejeicoes": rejeicoes, "confere": confere})
    db.session.commit()
    if caso.get("duplicar_evidencia"):
        original = PassagemEstimada.query.filter_by(parada_codigo=caso["parada"]).one()
        viagem = original.viagem
        outra = next(p for p in contexto["grades"][contexto["dias"][0]["data"], caso["sentido"]]
                     if p.id != viagem.partida_programada_id)
        duplicada = ViagemObservada(id=contexto["uid"]("viagem-duplicada"), partida_programada=outra,
            saida_observada=viagem.saida_observada, cobertura_tracado=1,
            versao_reconstrucao=RECON, versao_correspondencia=MATCH)
        db.session.add(PassagemEstimada(viagem=duplicada, versao_gtfs_id=original.versao_gtfs_id,
            parada_codigo=original.parada_codigo, instante=original.instante, metodo=original.metodo,
            versao_estimativa=original.versao_estimativa, posicao_anterior_id=original.posicao_anterior_id,
            posicao_posterior_id=original.posicao_posterior_id))
        db.session.commit()
    return referencias, originais


def conferir(caderno, caso):
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://",
                      "MYSQL_SSL_CA": None, "PASSAGEM_LACUNA_MAXIMA_SEG": None,
                      "LIMITES_RELOGIO": lambda: 120, "INDICADORES_TRUSTED_PROXY_CIDRS": ""})
    with app.app_context():
        try:
            exigir_sqlite_em_memoria()
            event.listen(db.engine, "connect", lambda c, _: c.execute("PRAGMA foreign_keys=ON"))
            db.create_all()
            contexto = _preparar(caderno, caso)
            passagens, originais = _estimar(caderno, caso, contexto)
            for dia, sentido, recon in sorted(contexto["contextos"]):
                campos = calcular_frequencias(linha=LINHA, sentido=sentido, data_operacao=date.fromisoformat(dia),
                    versao_gtfs_id=contexto["versao"].id, versao_correspondencia=MATCH,
                    versao_reconstrucao=recon, agora=AGORA, intervalo=60)
                for campo in campos:
                    if caso.get("omitir_observada") and campo["indicador"] == OBSERVADA and campo["parada_codigo"] == caso["parada"] and campo["periodo_inicio"] == datetime.fromisoformat(caso["omitir_observada"]):
                        continue  # interrupção de publicação CONTROLADA, prevista no caderno
                    db.session.add(IndicadorOperacional(**campo))
            db.session.commit()
            dias = contexto["dias"]
            a, b = caso.get("periodo_minutos", [540, 555])
            parametros = {"tipo": "FREQUENCIA", "linha": LINHA, "sentido": caso["sentido"],
                "parada": caso["parada"], "agregacao": caso["escala"],
                "inicio": instante(dias[0]["data"], a if caso["escala"] == "janela" else 0).isoformat(),
                "fim": (instante(dias[0]["data"], b) if caso["escala"] == "janela" else
                        instante(dias[-1]["data"], 1440)).isoformat()}
            if caso.get("faixa"):
                parametros.update(zip(("hora_inicio", "hora_fim"), caso["faixa"]))
            resposta = app.test_client().get("/indicadores", query_string=parametros)
            corpo = resposta.get_json()
            itens = sorted(corpo.get("dados", []), key=lambda i: (i["qualidade"]["versoes"]["versao_reconstrucao"], i["inicio"]))
            comparacoes = []

            def comparar(campo, manual, obtido, tolerancia=None):
                confere = (obtido is not None and abs(float(manual) - obtido) <= tolerancia
                           if tolerancia is not None else manual == obtido)
                comparacoes.append({"campo": campo, "manual": manual, "obtido": obtido, "confere": confere})

            comparar("http_status", 200, resposta.status_code)
            comparar("quantidade_itens", len(caso["esperado"]), len(itens))
            for indice, (manual, obtido) in enumerate(zip(caso["esperado"], itens)):
                comparar(f"{indice}.reconstrucao", manual.get("recon", RECON), obtido["qualidade"]["versoes"]["versao_reconstrucao"])
                comparar(f"{indice}.unidade", "passagens/h", obtido["unidade"])
                comparar(f"{indice}.sentido", caso["sentido"], obtido["sentido"])
                if "inicio_minuto" in manual:
                    comparar(f"{indice}.inicio", instante(dias[0]["data"], manual["inicio_minuto"]).isoformat(), obtido["inicio"])
                for lado in ("observada", "programada"):
                    ref, calculado = manual[lado], obtido[lado]
                    comparar(f"{indice}.{lado}.contagem", ref["n"], calculado["contagem"])
                    comparar(f"{indice}.{lado}.situacao", ref["situacao"], calculado["situacao"])
                    comparar(f"{indice}.{lado}.horas_publicadas", float(Fraction(ref["horas"])), calculado["cobertura"]["duracao_publicada_horas"], 1e-9)
                    taxa = None if ref["taxa"] is None else float(Fraction(ref["taxa"]))
                    comparar(f"{indice}.{lado}.taxa", taxa, calculado["valor"], None if taxa is None else caderno["tolerancia_taxa"])
                    if "parcial" in ref:
                        comparar(f"{indice}.{lado}.contagem_parcial", ref["parcial"], calculado["cobertura"]["contagem_valida_parcial"])
                    if "motivo" in ref:
                        comparar(f"{indice}.{lado}.motivo", True, ref["motivo"] in calculado["motivos"])
            preservados = all(tuple(db.session.execute(select(RegistroBrutoGPS.id, RegistroBrutoGPS.horario_gps,
                RegistroBrutoGPS.latitude, RegistroBrutoGPS.longitude).where(RegistroBrutoGPS.id == p[0])).one()) == p for p in originais)
            comparar("brutos_preservados", True, preservados)
            confere = all(c["confere"] for c in comparacoes) and all(p["confere"] for p in passagens)
            return {"id": caso["id"], "titulo": caso["titulo"], "escala": caso["escala"],
                "status": "CONFERIU_EXEMPLO" if confere else "DIVERGENTE",
                "aceite_institucional": "PENDENTE", "calculo_manual": caso["manual"],
                "consulta": parametros, "passagens": passagens, "comparacoes": comparacoes, "resposta_api": corpo}
        finally:
            db.session.remove()
            db.engine.dispose()


def executar(caderno=None):
    caderno = caderno or carregar_caderno()
    resultados = []
    for caso in caderno["casos"]:
        try:
            resultados.append(conferir(caderno, caso))
        except Exception as erro:
            resultados.append({"id": caso["id"], "titulo": caso["titulo"], "escala": caso["escala"],
                "status": "FALHA_EXECUCAO", "erro": f"{type(erro).__name__}: {erro}", "aceite_institucional": "PENDENTE"})
    commit = subprocess.run(["git", "-C", str(RAIZ), "rev-parse", "HEAD"], capture_output=True, text=True)
    return {"tarefa": "US15-T04", "origem": "SINTETICA_CONTROLADA", "aceite_institucional": "PENDENTE",
        "executado_em": datetime.now(UTC).isoformat(),
        "pronto": False, "fonte_casos": "docs/validacoes/US15-T04-casos.json",
        "sha256_caderno": hash_texto(CADERNO), "normalizacao_hash_texto": "UTF-8 com quebras LF",
        "codigo_base": commit.stdout.strip() if commit.returncode == 0 else None,
        "sha256_codigo": {p: hash_texto(RAIZ / p) for p in (
            "app/viagens/passagens.py", "app/indicadores/frequencia.py", "app/indicadores/routes.py",
            "scripts/validar_us15_t04.py")},
        "ambiente": {"python": platform.python_version(), "sistema": platform.system(),
                     "banco": "SQLite em memoria; FK ON; sem Aiven", "relogio_ensaio": AGORA.isoformat(),
                     "bibliotecas": {b: version(b) for b in ("Flask", "SQLAlchemy", "pyproj", "shapely")}},
        "resumo": dict(Counter(r["status"] for r in resultados)), "casos": resultados}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--saida", type=Path, default=RAIZ / "docs/evidencias/US15-T04-conferencia.json")
    args = parser.parse_args()
    relatorio = executar()
    args.saida.parent.mkdir(parents=True, exist_ok=True)
    args.saida.write_text(json.dumps(relatorio, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"resumo": relatorio["resumo"], "aceite": "PENDENTE", "pronto": False}, ensure_ascii=False))
    return 0 if all(r["status"] == "CONFERIU_EXEMPLO" for r in relatorio["casos"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
