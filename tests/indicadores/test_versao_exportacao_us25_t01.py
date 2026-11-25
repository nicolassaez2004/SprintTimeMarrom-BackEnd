"""US25-T01: a exportação usa exatamente a versão exibida no painel (dados SINTÉTICOS).

Linha 3762-10, partidas 12:00/12:15/12:30 UTC de 24/11/2026 (09:00 em São Paulo).
O painel é lido com a 1ª viagem apenas; depois chega a viagem atrasada de 12:15 e a
consolidação reescreve a janela. A versão antiga continua exportável sem mudanças.
"""

import csv
import io
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from app import db
from app.exportacoes import csv as exportador
from app.indicadores.agenda import ATUALIZACAO, atualizar_indicadores, consolidar_dia
from app.models import ExecucaoProcessamento, IndicadorOperacional, ViagemObservada

UTC = timezone.utc
T0 = datetime(2026, 11, 24, 12, tzinfo=UTC)
LINHA = {"linha": "3762-10", "sentido": "ida"}
DIA = {"formato": "csv", "data": "2026-11-24", **LINHA}


def _viagem(partida, atraso=30):
    db.session.add(ViagemObservada(
        partida_programada=partida, saida_observada=partida.saida_prevista + timedelta(seconds=atraso),
        cobertura_tracado=0.9, versao_reconstrucao="recon-1", versao_correspondencia="match-1"))
    db.session.commit()


@pytest.fixture
def painel(programacao, app):
    """Duas atualizações de 15 min publicadas e o painel lido em seguida."""
    _, _, _, partidas = programacao
    _viagem(partidas[0])
    atualizar_indicadores(T0 + timedelta(minutes=16))   # janela 12:00–12:15
    atualizar_indicadores(T0 + timedelta(minutes=31))   # janela 12:15–12:30
    cliente = app.test_client()
    corpo = cliente.get("/series", query_string=LINHA).get_json()
    return cliente, partidas, corpo


def _serie(cliente, consulta):
    """Todas as páginas de /series (o painel pagina em até 100 pontos)."""
    corpo = cliente.get("/series", query_string={**consulta, "page": 1}).get_json()
    pagina = 1
    while len(corpo["dados"]) < corpo["paginacao"]["total"]:
        pagina += 1
        corpo["dados"] += cliente.get("/series", query_string={**consulta, "page": pagina}).get_json()["dados"]
    return corpo


def _csv(resposta):
    assert resposta.status_code == 200, resposta.get_json()
    return list(csv.DictReader(io.StringIO(resposta.data.decode("utf-8-sig"))))


def _num(texto):
    return None if texto == "" else float(texto)


def _como_csv(item):
    """Ponto de /series nos termos do CSV (vazio = NULL)."""
    return {
        "linha": item["linha"], "sentido": item["sentido"] or "", "tipo": item["tipo"],
        "valor": item["valor"], "unidade": item["unidade"], "inicio": item["inicio"], "fim": item["fim"],
        "parada": item["parada"] or "", "trecho": item["trecho"] or "",
        "versao_gtfs_id": item["versao_gtfs_id"], "versao_conjunto": item["versao_conjunto"],
        "versao_correspondencia": item["versao_correspondencia"] or "",
        "versao_calculo": item["versao_calculo"] or "",
        "contagem_numerador": item["contagens"]["numerador"],
        "contagem_denominador": item["contagens"]["denominador"],
        "qualidade": item["qualidade"],
    }


def _normalizar(linha):
    return {**{c: linha[c] for c in (
        "linha", "sentido", "tipo", "unidade", "inicio", "fim", "parada", "trecho", "versao_gtfs_id",
        "versao_conjunto", "versao_correspondencia", "versao_calculo")},
        "valor": _num(linha["valor"]),
        "contagem_numerador": None if linha["contagem_numerador"] == "" else int(linha["contagem_numerador"]),
        "contagem_denominador": (None if linha["contagem_denominador"] == ""
                                 else int(linha["contagem_denominador"])),
        "qualidade": json.loads(linha["qualidade"])}


def _exportar(cliente, versao, **extra):
    return cliente.get("/downloads/indicadores", query_string={**DIA, "versao_dados": versao, **extra})


def test_series_informa_a_versao_do_conjunto_exibido(painel):
    _, _, corpo = painel
    ultima = ExecucaoProcessamento.query.filter_by(tipo=ATUALIZACAO).order_by(
        ExecucaoProcessamento.periodo_inicio.desc()).first()
    assert corpo["versao_dados"] == {
        "id": ultima.id, "tipo": ATUALIZACAO, "publicada_em": "2026-11-24T12:31:00+00:00",
        "periodo_inicio": "2026-11-24T12:15:00+00:00", "periodo_fim": "2026-11-24T12:30:00+00:00"}
    assert exportador.caminho_conjunto(ultima.id).exists()


def test_exportacao_da_versao_reproduz_o_painel_campo_a_campo(painel):
    cliente, _, corpo = painel
    linhas = _csv(_exportar(cliente, corpo["versao_dados"]["id"]))
    assert len(linhas) == len(corpo["dados"]) > 0
    assert [_normalizar(r) for r in linhas] == [_como_csv(i) for i in corpo["dados"]]
    # NULL continua vazio (não vira zero) e fonte/licença acompanham cada linha.
    nulos = [i for i in corpo["dados"] if i["valor"] is None]
    assert nulos and all(r["valor"] == "" for r in linhas if r["tipo"] in {i["tipo"] for i in nulos}
                         and r["inicio"] in {i["inicio"] for i in nulos})
    assert {r["fonte"] for r in linhas} == {exportador.FONTE}
    assert {r["licenca"] for r in linhas} == {exportador.LICENCA}


def test_nova_publicacao_nao_altera_a_exportacao_da_versao_anterior(painel):
    cliente, partidas, antes = painel
    versao_antiga = antes["versao_dados"]["id"]
    exportado_antes = _exportar(cliente, versao_antiga).data

    _viagem(partidas[1], atraso=0)  # dado atrasado: a janela 12:15 passa a ser cumprida
    consolidar_dia(date(2026, 11, 24), datetime(2026, 11, 25, 7, 1, tzinfo=UTC))
    depois = _serie(cliente, LINHA)
    assert depois["versao_dados"]["id"] != versao_antiga

    chave = ("CUMPRIMENTO_VIAGENS", "2026-11-24T12:15:00+00:00")
    valor = {i: {(p["tipo"], p["inicio"]): p["valor"] for p in c["dados"]} for i, c in
             (("antes", antes), ("depois", depois))}
    assert (valor["antes"][chave], valor["depois"][chave]) == (0.0, 100.0)

    antigo = _exportar(cliente, versao_antiga)
    assert antigo.data == exportado_antes  # byte a byte
    assert antigo.headers["X-Versao-Dados"] == versao_antiga
    assert {(r["tipo"], r["inicio"]): _num(r["valor"]) for r in _csv(antigo)}[chave] == 0.0
    # Sem mistura: a exportação antiga não contém conjuntos da consolidação, e vice-versa.
    novo = _csv(_exportar(cliente, depois["versao_dados"]["id"]))
    assert [_normalizar(r) for r in novo] == [_como_csv(i) for i in depois["dados"]]
    conjuntos_antigos = {r["versao_conjunto"] for r in _csv(antigo)}
    assert conjuntos_antigos == {i["versao_conjunto"] for i in antes["dados"]}
    assert len(novo) == 96 * len({i["tipo"] for i in depois["dados"]})
    # A tabela atual mudou; a versão antiga não foi lida dela.
    atual = IndicadorOperacional.query.filter_by(
        indicador="CUMPRIMENTO_VIAGENS", periodo_inicio=T0 + timedelta(minutes=15)).one()
    assert atual.valor == 100.0


def test_filtros_combinados_faixa_horaria_e_periodo(painel):
    cliente, _, corpo = painel
    versao = corpo["versao_dados"]["id"]
    consulta = {**LINHA, "tipo": "CUMPRIMENTO_VIAGENS", "hora_inicio": "09:00", "hora_fim": "09:15",
                "inicio": "2026-11-24T00:00:00-03:00", "fim": "2026-11-25T00:00:00-03:00"}
    serie = cliente.get("/series", query_string=consulta).get_json()
    resposta = cliente.get("/downloads/indicadores", query_string={
        "formato": "csv", "versao_dados": versao, **consulta})
    linhas = _csv(resposta)
    assert [_normalizar(r) for r in linhas] == [_como_csv(i) for i in serie["dados"]]
    assert [(r["tipo"], r["inicio"]) for r in linhas] == [
        ("CUMPRIMENTO_VIAGENS", "2026-11-24T12:00:00+00:00")]
    assert resposta.headers["X-Total-Registros"] == "1"
    assert 'filename="indicadores_2026-11-24_2026-11-25_v' in resposta.headers["Content-Disposition"]

    # Período de vários dias é aceito com versão (até 31 dias).
    varios = cliente.get("/downloads/indicadores", query_string={
        "formato": "csv", "versao_dados": versao, **LINHA,
        "inicio": "2026-11-20T00:00:00-03:00", "fim": "2026-11-27T00:00:00-03:00"})
    assert len(_csv(varios)) == len(corpo["dados"])
    longo = cliente.get("/downloads/indicadores", query_string={
        "formato": "csv", "versao_dados": versao,
        "inicio": "2026-10-01T00:00:00-03:00", "fim": "2026-11-27T00:00:00-03:00"})
    assert longo.status_code == 400


def test_versao_inexistente_ou_expirada_retorna_erro_explicito(painel):
    cliente, _, corpo = painel
    versao = corpo["versao_dados"]["id"]
    inexistente = _exportar(cliente, str(uuid4()))
    assert inexistente.status_code == 404
    assert inexistente.get_json()["erro"]["codigo"] == "VERSAO_INEXISTENTE"
    assert _exportar(cliente, "abc").status_code == 400

    # Arquivo adulterado e, depois, removido (expirado): 410, sem trocar de versão.
    arquivo = exportador.caminho_conjunto(versao)
    arquivo.write_bytes(arquivo.read_bytes() + b"x")
    adulterado = _exportar(cliente, versao)
    assert adulterado.status_code == 410
    assert adulterado.get_json()["erro"]["motivo"] == "ARQUIVO_AUSENTE_OU_INVALIDO"
    arquivo.unlink()
    expirado = _exportar(cliente, versao).get_json()["erro"]
    assert (expirado["codigo"], expirado["versao_dados"]) == ("VERSAO_INDISPONIVEL", versao)


def test_registro_publicado_fora_da_agenda_torna_a_versao_irreproduzivel(painel):
    cliente, _, corpo = painel
    modelo = IndicadorOperacional.query.filter_by(indicador="CUMPRIMENTO_VIAGENS").first()
    db.session.add(IndicadorOperacional(
        versao_gtfs_id=modelo.versao_gtfs_id, linha_codigo=modelo.linha_codigo, sentido="0",
        indicador="CUMPRIMENTO_VIAGENS", valor=50.0, unidade="%",
        periodo_inicio=T0 + timedelta(hours=2), periodo_fim=T0 + timedelta(hours=2, minutes=15),
        versao_conjunto="manual", publicado_em=T0 + timedelta(minutes=20)))
    db.session.commit()
    erro = _exportar(cliente, corpo["versao_dados"]["id"]).get_json()["erro"]
    assert (erro["codigo"], erro["motivo"]) == ("VERSAO_INDISPONIVEL", "PERIODO_NAO_PRESERVADO")
    # Filtrando outro tipo, o registro não pertence à exportação pedida.
    assert _exportar(cliente, corpo["versao_dados"]["id"], tipo="PONTUALIDADE_VIAGENS").status_code == 200


def test_falha_ao_preservar_desfaz_a_publicacao(programacao, app, monkeypatch):
    _viagem(programacao[3][0])

    def quebra(_execucao):
        raise OSError("disco cheio")

    monkeypatch.setattr(exportador, "preservar_conjunto", quebra)
    with pytest.raises(OSError):
        atualizar_indicadores(T0 + timedelta(minutes=16))
    assert IndicadorOperacional.query.count() == 0
    assert ExecucaoProcessamento.query.one().estado == "ERRO"
    assert app.test_client().get("/series", query_string=LINHA).get_json()["versao_dados"] is None


def test_compatibilidade_indicadores_e_download_sem_versao(painel):
    cliente, _, corpo = painel
    assert cliente.get("/indicadores", query_string=LINHA).get_json()["versao_dados"] == corpo["versao_dados"]
    # Sem versao_dados, faixa horária continua recusada e o dia não consolidado segue 404.
    assert cliente.get("/downloads/indicadores", query_string={
        **DIA, "hora_inicio": "09:00", "hora_fim": "10:00"}).status_code == 400
    legado = cliente.get("/downloads/indicadores", query_string=DIA)
    assert legado.status_code == 404 and "X-Versao-Dados" not in legado.headers
    consolidar_dia(date(2026, 11, 24), datetime(2026, 11, 25, 7, 1, tzinfo=UTC))
    exportador.publicar_csv_indicadores(date(2026, 11, 24), datetime(2026, 11, 25, 7, 1, tzinfo=UTC))
    legado = cliente.get("/downloads/indicadores", query_string=DIA)
    assert legado.status_code == 200
    versao = cliente.get("/series", query_string=LINHA).get_json()["versao_dados"]["id"]
    # Após a consolidação, a versão vigente de um dia inteiro coincide com o CSV diário (US10).
    assert _csv(legado) == _csv(_exportar(cliente, versao))
