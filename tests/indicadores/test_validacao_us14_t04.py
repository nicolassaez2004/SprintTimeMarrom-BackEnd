"""US14-T04: vinte casos de cumprimento com cálculo manual x sistema.

Dados SINTÉTICOS, construídos para exercitar as regras; não são programação nem
viagens reais da SPTrans e não substituem os casos aprovados pela Instituição
Parceira. O resultado manual de cada caso foi calculado à mão e está escrito
como literal; o sistema é executado pelo fluxo persistido completo
(ImportacaoProgramacao -> PartidaProgramada -> ViagemObservada ->
IndicadorOperacional).

Gerar a tabela de evidência (banco SQLite em memória, sem rede); `--csv`
regrava docs/evidencias/US14-T04-casos.csv:
    DATABASE_URL=sqlite:// python -m tests.indicadores.test_validacao_us14_t04 [--csv]
"""

import csv
import io
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app import create_app, db
from app.indicadores.cumprimento import (
    calcular_cumprimento,
    calcular_e_publicar_cumprimento,
    viagens_observadas,
)
from app.models import (
    ArquivoArmazenado,
    ImportacaoProgramacao,
    Linha,
    PartidaProgramada,
    VersaoGTFS,
    ViagemObservada,
)
from app.repositories.programacao import consultar_por_dia

SP = ZoneInfo("America/Sao_Paulo")
LINHA = "VAL-10"
RECONSTRUCAO, CORRESPONDENCIA = "recon-val-1", "corr-val-1"
CAPTURA = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)

# Grade por (data operacional, sentido, fonte, oferta): minutos desde 00:00
# local -> (atraso em segundos, cobertura) da viagem observada, ou None.
# 1450 min = 24:10 GTFS (00:10 de 25/11), ainda no dia operacional 24/11.
GRADES = {
    (date(2026, 11, 24), "0", "OFICIAL", "FIXA"): {
        300: (600, 0.80), 320: (0, 0.79), 340: (-601, 1.0), 360: (300, 0.90),
        370: (301, 0.90), 380: None, 390: (-300, 0.85), 420: (900, 1.0),
        480: (901, 1.0), 1410: (-900, 0.95), 1450: (120, 0.80),
    },
    (date(2026, 11, 24), "1", "OFICIAL", "FIXA"): {360: (0, 1.0), 390: (0, 1.0)},
    (date(2026, 11, 25), "0", "OFICIAL", "FIXA"): {300: (900, 1.0), 330: (60, 1.0)},
    # Captura GTFS mais recente do mesmo dia: deve ser ignorada (OFICIAL prevalece).
    (date(2026, 11, 25), "0", "GTFS", "FIXA"): {300: (0, 1.0), 315: (0, 1.0), 330: (0, 1.0)},
    (date(2026, 11, 26), "0", "OFICIAL", "FREQUENCIA"): {
        360: (300, 1.0), 370: (301, 1.0), 380: (-300, 1.0), 390: (0, 0.80),
        400: (0, 0.50), 410: None,
    },
}
# Viagem de outra versão de reconstrução para a partida 24/11 06:20: não conta.
OUTRA_VERSAO = (date(2026, 11, 24), "0", 380)


def _local(dia, minutos):
    return datetime(dia.year, dia.month, dia.day, tzinfo=SP) + timedelta(minutes=minutos)


def _h(dia, hora, minuto=0):
    return datetime(dia.year, dia.month, dia.day, hora, minuto, tzinfo=SP)


D24, D25, D26 = date(2026, 11, 24), date(2026, 11, 25), date(2026, 11, 26)

# (n, tipo de janela, sentido, início local, fim local,
#  cumpridas manual, programadas manual, % manual, conta manual)
CASOS = [
    (1, "15 min", "0", _h(D24, 5), _h(D24, 5, 15), 1, 1, "100.00",
     "05:00 intervalo 20 min -> tol 600 s; +600 s e cobertura 80% -> cumprida"),
    (2, "15 min", "0", _h(D24, 5, 15), _h(D24, 5, 30), 0, 1, "0.00",
     "05:20 cobertura 79% < 80% -> não cumprida"),
    (3, "15 min", "0", _h(D24, 5, 30), _h(D24, 5, 45), 0, 1, "0.00",
     "05:40 intervalo 20 min -> tol 600 s; -601 s -> não cumprida"),
    (4, "15 min", "0", _h(D24, 6), _h(D24, 6, 15), 1, 2, "50.00",
     "06:00 e 06:10 intervalo 10 min -> tol 300 s; +300 s sim, +301 s não"),
    (5, "15 min", "0", _h(D24, 6, 15), _h(D24, 6, 30), 0, 1, "0.00",
     "06:20 sem viagem na versão recon-val-1 (só em outra versão) -> não cumprida"),
    (6, "15 min", "0", _h(D24, 6, 30), _h(D24, 6, 45), 1, 1, "100.00",
     "06:30 intervalo 10 min -> tol 300 s; -300 s -> cumprida"),
    (7, "15 min", "0", _h(D24, 7), _h(D24, 7, 15), 1, 1, "100.00",
     "07:00 intervalo 30 min -> tol 900 s; +900 s -> cumprida"),
    (8, "15 min", "0", _h(D24, 8), _h(D24, 8, 15), 0, 1, "0.00",
     "08:00 intervalo 60 min -> metade 1800 s limitada a 900 s; +901 s -> não"),
    (9, "15 min", "0", _h(D24, 10), _h(D24, 10, 15), 0, 0, None,
     "nenhuma partida programada -> denominador 0, valor NULL"),
    (10, "15 min", "0", _h(D24, 23, 30), _h(D24, 23, 45), 1, 1, "100.00",
     "23:30 intervalo 40 min -> metade 1200 s limitada a 900 s; -900 s -> cumprida"),
    (11, "15 min", "0", _h(D25, 0), _h(D25, 0, 15), 1, 1, "100.00",
     "24:10 GTFS do dia 24/11 = 00:10 de 25/11; tol 900 s; +120 s -> cumprida"),
    (12, "faixa horária", "0", _h(D24, 5), _h(D24, 6), 1, 3, "33.33",
     "05:00 sim, 05:20 não, 05:40 não -> 1/3"),
    (13, "faixa horária", "0", _h(D24, 6), _h(D24, 7), 2, 4, "50.00",
     "06:00 sim, 06:10 não, 06:20 não, 06:30 sim -> 2/4"),
    (14, "faixa horária", "0", _h(D24, 6), _h(D24, 9), 3, 6, "50.00",
     "06:00, 06:30, 07:00 sim; 06:10, 06:20, 08:00 não -> 3/6"),
    (15, "dia", "0", _h(D24, 0), _h(D25, 0), 5, 10, "50.00",
     "partidas 05:00..23:30 do dia 24/11 (24:10 cai em 25/11) -> 5/10"),
    (16, "dia", "0", _h(D25, 0), _h(D26, 0), 3, 3, "100.00",
     "24:10 de 24/11 + OFICIAL 25/11 05:00 (+900 s, tol 900 s) e 05:30; GTFS ignorada -> 3/3"),
    (17, "dia", "0", _h(D26, 0), _h(date(2026, 11, 27), 0), 3, 6, "50.00",
     "frequência 600 s -> tol 300 s: +300 sim, +301 não, -300 sim, cob. 80% sim, cob. 50% não, sem viagem"),
    (18, "faixa horária", "0", _h(D26, 6), _h(D26, 6, 30), 2, 3, "66.67",
     "frequência: 06:00 sim, 06:10 não, 06:20 sim -> 2/3"),
    (19, "mês", "0", _h(date(2026, 11, 1), 0), _h(date(2026, 12, 1), 0), 11, 19, "57.89",
     "soma das contagens: 24/11 (6/11) + 25/11 (2/2) + 26/11 (3/6) = 11/19"),
    (20, "dia", "1", _h(D24, 0), _h(D25, 0), 2, 2, "100.00",
     "sentido 1 separado do sentido 0: 06:00 e 06:30 no horário -> 2/2"),
]


def montar_cenario():
    arquivo = ArquivoArmazenado(
        chave="validacao/us14-t04.zip", nome_original="us14-t04.zip",
        mime_type="application/zip", tamanho_bytes=0, sha256="c" * 64, criado_em=CAPTURA,
    )
    versao = VersaoGTFS(hash="c" * 64, capturada_em=CAPTURA, vigencia_inicio=date(2026, 11, 1),
                        vigencia_fim=date(2026, 12, 31), arquivo=arquivo)
    linha = Linha(versao_gtfs=versao, codigo=LINHA, nome="Linha sintética de validação")
    db.session.add_all([versao, linha])
    db.session.flush()
    for (dia, sentido, fonte, oferta), grade in GRADES.items():
        importacao = ImportacaoProgramacao(
            versao_gtfs_id=versao.id, linha=linha, data_operacao=dia, sentido=sentido,
            tipo_dia="UTIL", fonte=fonte, versao_fonte=f"{fonte.lower()}-{dia.isoformat()}",
            fonte_url="https://example.invalid/validacao", capturada_em=CAPTURA,
            # GTFS verificada depois da OFICIAL: a prioridade da fonte deve vencer.
            verificada_em=CAPTURA + timedelta(hours=1 if fonte == "GTFS" else 0),
            arquivo=arquivo,
        )
        db.session.add(importacao)
        for ordem, (minutos, observada) in enumerate(sorted(grade.items()), start=1):
            partida = PartidaProgramada(
                importacao=importacao, ordem=ordem, saida_prevista=_local(dia, minutos),
                tipo_oferta=oferta, intervalo_seg=600 if oferta == "FREQUENCIA" else None,
            )
            db.session.add(partida)
            db.session.flush()
            if (dia, sentido, minutos) == OUTRA_VERSAO:
                db.session.add(ViagemObservada(
                    partida_programada_id=partida.id, saida_observada=partida.saida_prevista,
                    cobertura_tracado=1.0, versao_reconstrucao="recon-val-0",
                    versao_correspondencia=CORRESPONDENCIA, qualidade={},
                ))
            if observada is not None:
                atraso, cobertura = observada
                db.session.add(ViagemObservada(
                    partida_programada_id=partida.id,
                    saida_observada=partida.saida_prevista + timedelta(seconds=atraso),
                    cobertura_tracado=cobertura, versao_reconstrucao=RECONSTRUCAO,
                    versao_correspondencia=CORRESPONDENCIA, qualidade={"sintetico": True},
                ))
    db.session.commit()
    return versao


def executar(versao, caso):
    _, _, sentido, inicio, fim, *_ = caso
    return calcular_e_publicar_cumprimento(
        linha=LINHA, sentido=sentido, inicio=inicio, fim=fim, versao_gtfs_id=versao.id,
        versao_correspondencia=CORRESPONDENCIA, versao_reconstrucao=RECONSTRUCAO,
    )


def _percentual(registro):
    return None if registro.valor is None else f"{registro.valor:.2f}"


def test_sao_vinte_casos_com_todas_as_janelas_exigidas():
    assert [c[0] for c in CASOS] == list(range(1, 21))
    assert {c[1] for c in CASOS} == {"15 min", "faixa horária", "dia", "mês"}


@pytest.mark.parametrize("caso", CASOS, ids=[f"caso{c[0]:02d}" for c in CASOS])
def test_sistema_confere_com_calculo_manual(app, caso):
    versao = montar_cenario()
    registro = executar(versao, caso)
    *_, cumpridas, programadas, percentual, _conta = caso
    assert (registro.contagem_numerador, registro.contagem_denominador) == (cumpridas, programadas)
    assert _percentual(registro) == percentual


def test_mes_soma_contagens_dos_dias_e_nao_media_de_percentuais(app):
    versao = montar_cenario()
    dias = [executar(versao, CASOS[n - 1]) for n in (15, 16, 17)]
    mes = executar(versao, CASOS[18])
    soma = (sum(d.contagem_numerador for d in dias), sum(d.contagem_denominador for d in dias))
    assert soma == (mes.contagem_numerador, mes.contagem_denominador) == (11, 19)
    media_simples = sum(d.valor for d in dias) / len(dias)
    assert round(media_simples, 2) == 66.67 != mes.valor == 57.89


def test_recalculo_e_reprodutivel(app):
    versao = montar_cenario()
    primeiro = {c[0]: executar(versao, c) for c in CASOS}
    resumo = {n: (r.chave_resultado, r.contagem_numerador, r.contagem_denominador, r.valor)
              for n, r in primeiro.items()}
    segundo = {c[0]: executar(versao, c) for c in CASOS}
    assert {n: (r.chave_resultado, r.contagem_numerador, r.contagem_denominador, r.valor)
            for n, r in segundo.items()} == resumo
    assert len({r.chave_resultado for r in segundo.values()}) == 20


def test_partida_apos_24h_mantem_data_operacional_na_rastreabilidade(app):
    """Caso 11: janela civil 25/11 00:00-00:15 usa a grade do dia operacional 24/11."""
    versao = montar_cenario()
    registro = executar(versao, CASOS[10])
    assert [c["data_operacao"] for c in registro.qualidade["capturas_programacao"]] == ["2026-11-24"]
    dia_25 = executar(versao, CASOS[15])
    assert sorted(c["data_operacao"] for c in dia_25.qualidade["capturas_programacao"]) == [
        "2026-11-24", "2026-11-25"]


# Alternativa para decisão do PO (não publicada): "dia" = dia operacional GTFS,
# com todas as partidas da grade do dia, inclusive as acima de 24h.
# (dia, sentido, cumpridas manual, programadas manual, % manual)
DIA_OPERACIONAL = [
    (D24, "0", 6, 11, "54.55"),  # civil (caso 15): 5/10 = 50.00
    (D25, "0", 2, 2, "100.00"),  # civil (caso 16): 3/3 = 100.00
    (D26, "0", 3, 6, "50.00"),   # civil (caso 17): 3/6 = 50.00
]


def calcular_dia_operacional(versao, dia, sentido):
    grade, _ = consultar_por_dia(versao.id, LINHA, sentido, dia)
    observadas = viagens_observadas(grade.partidas, RECONSTRUCAO, CORRESPONDENCIA)
    return calcular_cumprimento(grade.partidas, observadas, grade.partidas)


@pytest.mark.parametrize("dia,sentido,cumpridas,programadas,percentual", DIA_OPERACIONAL)
def test_alternativa_dia_operacional_confere_com_calculo_manual(
        app, dia, sentido, cumpridas, programadas, percentual):
    resultado = calcular_dia_operacional(montar_cenario(), dia, sentido)
    assert (resultado.cumpridas, resultado.programadas) == (cumpridas, programadas)
    assert f"{resultado.percentual}" == percentual


def linhas_evidencia(versao):
    """Uma linha por caso: programação de referência, manual e sistema."""
    linhas = []
    for caso in CASOS:
        n, janela, sentido, inicio, fim, cumpridas, programadas, percentual, conta = caso
        registro = executar(versao, caso)
        fontes = {c["importacao_id"] for c in registro.qualidade["capturas_programacao"]}
        partidas = (PartidaProgramada.query.filter(
            PartidaProgramada.importacao_id.in_(fontes),
            PartidaProgramada.saida_prevista >= inicio, PartidaProgramada.saida_prevista < fim,
        ).order_by(PartidaProgramada.saida_prevista).all()) if fontes else []
        referencia = []
        for p in partidas:
            v = ViagemObservada.query.filter_by(
                partida_programada_id=p.id, versao_reconstrucao=RECONSTRUCAO).one_or_none()
            hora = p.saida_prevista.astimezone(SP).strftime("%d/%m %H:%M")
            observado = "sem viagem" if v is None else (
                f"{(v.saida_observada - p.saida_prevista).total_seconds():+.0f} s / "
                f"{v.cobertura_tracado:.0%}")
            referencia.append(f"{hora}: {observado}")
        sistema = (registro.contagem_numerador, registro.contagem_denominador, _percentual(registro))
        linhas.append({
            "caso": n, "janela": janela, "sentido": sentido,
            "inicio_local": f"{inicio:%Y-%m-%d %H:%M}", "fim_local": f"{fim:%Y-%m-%d %H:%M}",
            "datas_operacao": " ".join(sorted(
                c["data_operacao"] for c in registro.qualidade["capturas_programacao"])),
            "programacao_referencia": referencia,
            "manual": f"{cumpridas}/{programadas} = {percentual or 'NULL'}", "conta_manual": conta,
            "sistema": f"{sistema[0]}/{sistema[1]} = {sistema[2] or 'NULL'}",
            "confere": "sim" if sistema == (cumpridas, programadas, percentual) else "NÃO",
        })
    return linhas


def tabela_markdown(linhas):
    saida = [
        ("| # | Janela | Sentido | Período (horário de São Paulo) | Dia(s) operacional(is)"
         " da grade | Programação de referência (partida: atraso s / cobertura)"
         " | Cálculo manual | Sistema | Confere |"),
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for item in linhas:
        referencia = item["programacao_referencia"]
        if len(referencia) > 6:
            referencia = referencia[:3] + [f"… ({len(referencia)} partidas)"]
        inicio, fim = (datetime.strptime(item[c], "%Y-%m-%d %H:%M") for c in ("inicio_local", "fim_local"))
        saida.append(
            f"| {item['caso']} | {item['janela']} | {item['sentido']} "
            f"| {inicio:%d/%m %H:%M} – {fim:%d/%m %H:%M} | {item['datas_operacao'] or '—'} "
            f"| {'<br>'.join(referencia) or '—'} "
            f"| {item['manual']}<br><small>{item['conta_manual']}</small> "
            f"| {item['sistema']} | {item['confere']} |"
        )
    return "\n".join(saida)


def csv_texto(linhas):
    """Planilha para a Instituição: as duas últimas colunas ficam em branco."""
    arquivo = io.StringIO()
    campos = ["caso", "janela", "sentido", "inicio_local", "fim_local", "datas_operacao",
              "programacao_referencia", "manual", "conta_manual", "sistema", "confere",
              "conferencia_instituicao", "observacao_instituicao"]
    escritor = csv.DictWriter(arquivo, fieldnames=campos, lineterminator="\n")
    escritor.writeheader()
    for item in linhas:
        escritor.writerow({**item, "programacao_referencia": "; ".join(item["programacao_referencia"]),
                           "conferencia_instituicao": "", "observacao_instituicao": ""})
    return arquivo.getvalue()


DOCUMENTO = Path(__file__).resolve().parents[2] / "docs/US14-T04-validacao.md"
PLANILHA = Path(__file__).resolve().parents[2] / "docs/evidencias/US14-T04-casos.csv"


def test_evidencias_publicadas_correspondem_ao_sistema(app):
    """Documento e planilha não podem divergir do cálculo atual."""
    linhas = linhas_evidencia(montar_cenario())
    assert all(item["confere"] == "sim" for item in linhas)
    assert tabela_markdown(linhas) in DOCUMENTO.read_text(encoding="utf-8")
    assert PLANILHA.read_text(encoding="utf-8") == csv_texto(linhas)


if __name__ == "__main__":
    import sys
    aplicacao = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://",
                            "MYSQL_SSL_CA": None})
    with aplicacao.app_context():
        db.create_all()
        evidencia = linhas_evidencia(montar_cenario())
    if "--csv" in sys.argv:
        PLANILHA.write_text(csv_texto(evidencia), encoding="utf-8")
    print(tabela_markdown(evidencia))
