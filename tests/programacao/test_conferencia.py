"""A referência de dados precisa detectar diferenças, não aprová-las por padrão."""

import zipfile
from datetime import date

import pytest

from scripts.conferir_partidas_gtfs import ReferenciaGTFS, comparar, montar_amostra

HASH = "a" * 64


@pytest.fixture
def referencia(tmp_path):
    caminho = tmp_path / "referencia-sintetica.zip"
    conteudos = {
        "routes.txt": "route_id,route_long_name\n1012-10,Linha sintética\n",
        "calendar.txt": "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\nU,1,1,1,1,1,0,0,20261101,20261130\n",
        "calendar_dates.txt": "service_id,date,exception_type\nU,20261121,1\nU,20261119,2\n",
        "trips.txt": "route_id,service_id,trip_id,direction_id\n1012-10,U,fixa,0\n1012-10,U,frequencia,0\n",
        "stop_times.txt": "trip_id,departure_time,stop_sequence\nfixa,06:00:00,1\nfrequencia,23:50:00,1\n",
        "frequencies.txt": "trip_id,start_time,end_time,headway_secs\nfrequencia,23:50:00,25:00:00,1800\n",
    }
    with zipfile.ZipFile(caminho, "w") as arquivo:
        for nome, conteudo in conteudos.items():
            arquivo.writestr(nome, conteudo)
    return ReferenciaGTFS(caminho)


def test_contagem_independente_cobre_fixa_frequencia_limite_exclusivo_e_excecoes(referencia):
    # A exceção inclui sábado: uma saída fixa e três na faixa de frequência.
    sabado = referencia.contar("1012-10", "0", date(2026, 11, 21))
    assert sabado["total"] == 4 and sabado["por_tipo"] == {"FIXA": 1, "FREQUENCIA": 3}
    assert sabado["referencias"][1]["frequencies_linhas"] == [2]
    # A outra exceção remove quinta-feira; domingo é inativo no calendário base.
    for dia in (date(2026, 11, 19), date(2026, 11, 22)):
        assert referencia.contar("1012-10", "0", dia)["total"] == 0
    assert referencia.contar("1012-10", "1", date(2026, 11, 21))["total"] == 0


def test_linha_inexistente_e_amostra_insuficiente_nao_sao_totais_zero(referencia):
    with pytest.raises(ValueError, match="ausente"):
        referencia.contar("inexistente", "0", date(2026, 11, 21))
    with pytest.raises(ValueError, match="menos de 30"):
        montar_amostra(referencia, date(2026, 11, 19))


def resposta(tipos=("FREQUENCIA", "FREQUENCIA"), **mudancas):
    base = {"quantidade": len(tipos), "partidas": [{"tipo_oferta": t} for t in tipos],
            "estado": "DISPONIVEL", "procedencia": {"fonte": "GTFS", "versao_fonte": HASH}}
    base.update(mudancas)
    return base


def test_mesmo_total_com_tipo_errado_permanece_divergente_e_nao_aprovado():
    esperado = {"total": 2, "por_tipo": {"FIXA": 0, "FREQUENCIA": 2}}
    resultado = comparar(esperado, 200, resposta(("FIXA", "FIXA")), HASH)
    assert resultado["estado"] == "DIVERGENTE" and resultado["aprovado"] is False
    assert resultado["diferenca"] == 0 and "TIPOS_DIVERGENTES" in resultado["diferencas"]


@pytest.mark.parametrize("mudanca,codigo", [
    ({"quantidade": 3}, "TOTAL_DIVERGENTE"),
    ({"procedencia": {"fonte": "GTFS", "versao_fonte": "b" * 64}}, "PROCEDENCIA_DIVERGENTE"),
    ({"estado": "NAO_IMPORTADA"}, "GRADE_NAO_PERSISTIDA"),
])
def test_divergencias_ficam_abertas(mudanca, codigo):
    resultado = comparar({"total": 2, "por_tipo": {"FREQUENCIA": 2}}, 200, resposta(**mudanca), HASH)
    assert resultado["estado"] == "DIVERGENTE" and codigo in resultado["diferencas"]
    assert resultado["aprovado"] is False


def test_erro_http_nao_vira_igualdade_de_zeros_e_conferencia_nao_e_aceite():
    esperado = {"total": 0, "por_tipo": {"FIXA": 0, "FREQUENCIA": 0}}
    erro = comparar(esperado, 503, {"erro": "indisponível"}, HASH)
    assert erro["estado"] == "ERRO_CONSULTA" and erro["total_api"] is None and erro["diferenca"] is None
    valida = comparar(esperado, 200, resposta((), estado="SEM_PARTIDAS"), HASH)
    assert valida["estado"] == "CONFERE_LOCALMENTE" and valida["aprovado"] is False
