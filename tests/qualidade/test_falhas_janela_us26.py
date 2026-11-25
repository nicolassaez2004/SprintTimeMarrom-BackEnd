"""US26-T01: percentual de falhas de coleta da janela exibida (dados SINTÉTICOS, SQLite).

Intervalo de 30 s: uma janela de 15 min espera 30 ciclos.
"""

from datetime import timedelta

import pytest

from app import db
from app.indicadores.cumprimento import gravar_indicador
from app.models import ExecucaoColeta, IndicadorOperacional, Linha, ObservacaoTratada, RegistroBrutoGPS
from app.qualidade.service import FalhasColeta, VERSAO_REGRA_FALHAS
from tests.qualidade.test_qualidade_coleta import D0, DIA, app, ciclos, lacuna, utc, versao  # noqa: F401

M15 = timedelta(minutes=15)
DEPOIS = D0 + timedelta(days=1)


def falhas(inicio=D0, fim=D0 + M15, agora=DEPOIS, **kw):
    return FalhasColeta(agora=agora, intervalo=30).da_janela(inicio, fim, **kw)


def coerente(r):
    """Contagens e percentual contam a mesma história."""
    assert r["falhas"] == r["ciclos_falha"] + r["ciclos_em_andamento"] + r["sem_registro"]
    assert r["falhas"] == r["denominador"] - r["ciclos_com_dados"]
    if r["denominador"]:
        assert r["percentual_falhas"] == round(r["falhas"] * 100 / r["denominador"], 2)


def test_exatamente_dez_por_cento_nao_alerta(app):
    ciclos(range(27))  # 3 de 30 sem registro
    r = falhas()
    assert (r["percentual_falhas"], r["incompleto"], r["situacao"]) == (10.0, False, "COM_FALHAS")
    assert r["motivo"] is None and r["sem_registro"] == 3
    coerente(r)


def test_acima_de_dez_por_cento_alerta(app):
    ciclos(range(26))
    ciclos(range(26, 30), estado="FALHA", erro="HTTP 503")  # registrados, mas sem dados
    r = falhas()
    assert r["percentual_falhas"] == 13.33 and r["incompleto"] is True
    assert (r["situacao"], r["motivo"]) == ("INCOMPLETA", "FALHAS_ACIMA_DO_LIMITE")
    assert (r["ciclos_falha"], r["sem_registro"], r["denominador"]) == (4, 0, 30)
    coerente(r)


def test_zero_falhas_com_ciclos_esperados(app):
    ciclos(range(30))
    r = falhas()
    assert (r["percentual_falhas"], r["incompleto"], r["situacao"]) == (0.0, False, "COMPLETA")
    assert r["ciclos_esperados"] == 30
    coerente(r)


def test_monitoramento_sem_nenhum_ciclo_com_dados_e_falha_total(app):
    ciclos([0])  # monitoramento começou; janela seguinte inteira em silêncio
    r = falhas(D0 + M15, D0 + 2 * M15)
    assert (r["percentual_falhas"], r["incompleto"]) == (100.0, True)
    assert (r["situacao"], r["motivo"]) == ("SEM_DADOS", "NENHUM_CICLO_COM_DADOS")
    coerente(r)


@pytest.mark.parametrize("inicio, fim, agora, motivo", [
    (D0, D0 + M15, DEPOIS, "FORA_DO_MONITORAMENTO"),                           # antes do 1º ciclo
    (D0 + 4 * M15, D0 + 5 * M15, D0 + 3 * M15, "JANELA_FUTURA"),               # ainda não ocorreu
    (D0 + timedelta(minutes=5), D0 + timedelta(minutes=20), DEPOIS, "JANELA_FORA_DA_GRADE_15MIN"),
])
def test_sem_expectativa_de_coleta_e_null_com_motivo(app, inicio, fim, agora, motivo):
    ciclos(range(120, 180))  # coleta só a partir de 01:00
    r = falhas(inicio, fim, agora=agora)
    assert r["percentual_falhas"] is None and r["incompleto"] is None
    assert (r["situacao"], r["motivo"], r["denominador"]) == ("SEM_EXPECTATIVA", motivo, 0)


def test_janela_parcialmente_futura_mede_so_o_observado(app):
    ciclos(range(30))
    r = falhas(D0, D0 + 2 * M15, agora=D0 + M15)
    assert (r["janelas_15min"], r["janelas_sem_expectativa"]) == (2, 1)
    assert (r["percentual_falhas"], r["denominador"]) == (0.0, 30)


def test_lacuna_aberta_e_ciclos_faltantes(app):
    ciclos(range(20))
    registrada = lacuna(D0 + timedelta(minutes=10), None)
    r = falhas(agora=D0 + timedelta(hours=1))
    assert (r["sem_registro"], r["percentual_falhas"], r["incompleto"]) == (10, 33.33, True)
    assert [(l["id"], l["origem"], l["estado"]) for l in r["lacunas"]] == [
        (registrada.id, "REGISTRADA", "ABERTA")]
    coerente(r)


def test_silencio_sem_ciclo_vira_lacuna_inferida_encerrada(app):
    ciclos([*range(10), *range(20, 30)])
    r = falhas()
    assert r["percentual_falhas"] == 33.33
    assert [(l["origem"], l["causa"], l["estado"]) for l in r["lacunas"]] == [
        ("INFERIDA", "SEM_CICLO_REGISTRADO", "ENCERRADA")]


def test_limite_da_janela_de_15_min_e_semiaberto(app):
    ciclos(range(30))
    ciclos([30])  # D0+15:00 pertence à janela seguinte
    assert falhas()["ciclos_registrados"] == 30
    seguinte = falhas(D0 + M15, D0 + 2 * M15)
    assert (seguinte["ciclos_registrados"], seguinte["sem_registro"]) == (1, 29)


def test_soma_contagens_e_nao_media_de_percentuais(app):
    # Monitoramento começa às 00:07:30: 1ª janela espera 15; 2ª janela, 30 com 6 faltando.
    ciclos([*range(15, 30), *range(30, 54)])
    r = falhas(D0, D0 + 2 * M15)
    assert (r["falhas"], r["denominador"]) == (6, 45)
    assert r["percentual_falhas"] == 13.33 and r["incompleto"] is True  # média daria 10%
    coerente(r)


def test_janela_que_atravessa_a_meia_noite(app):
    ciclos(range(2850, 2910))  # 23:45 → 00:15 do dia seguinte
    r = falhas(D0 + timedelta(hours=23, minutes=45), DEPOIS + M15, agora=DEPOIS + timedelta(days=1))
    assert (r["janelas_15min"], r["denominador"], r["percentual_falhas"]) == (2, 60, 0.0)


def test_escopo_global_aplicado_a_linha_e_ausencia_operacional(app):
    ciclos(range(30))
    r = falhas(linha="8000-10")
    # Ciclos são da frota: o percentual não é da linha, apenas exibido junto dela.
    assert (r["escopo"], r["linha_exibida"], r["versao_regra"]) == (
        "COLETA_GLOBAL", "8000-10", VERSAO_REGRA_FALHAS)
    assert r["operacao_linha"] == "AUSENCIA_OPERACIONAL" and r["percentual_falhas"] == 0.0
    assert falhas()["operacao_linha"] is None  # sem linha, nada a afirmar


def test_posicoes_da_linha_e_correspondencia_pendente(app):
    ciclos(range(30))
    execucao = ExecucaoColeta.query.first()

    def bruto():
        registro = RegistroBrutoGPS(execucao=execucao, coletado_em=D0 + timedelta(minutes=1))
        db.session.add(registro)
        db.session.commit()
        return registro

    bruto()
    assert falhas(linha="8000-10")["operacao_linha"] == "CORRESPONDENCIA_PENDENTE"
    db.session.add(ObservacaoTratada(bruto=RegistroBrutoGPS.query.one(), linha_codigo="9999-10",
                                     versao_correspondencia="v1"))
    db.session.commit()
    assert falhas(linha="8000-10")["operacao_linha"] == "AUSENCIA_OPERACIONAL"
    db.session.add(ObservacaoTratada(bruto=bruto(), linha_codigo="8000-10", versao_correspondencia="v1"))
    db.session.commit()
    assert falhas(linha="8000-10")["operacao_linha"] == "COM_POSICOES"


def test_publicacao_persiste_bloco_sem_perder_qualidade_do_indicador(app):
    from datetime import date
    gtfs = versao(date(2026, 1, 1), None, "a")
    db.session.add(Linha(versao_gtfs_id=gtfs.id, codigo="8000-10", nome="Teste"))
    ciclos(range(26))
    campos = {"chave_resultado": "k1", "versao_gtfs_id": gtfs.id, "linha_codigo": "8000-10",
              "sentido": "0", "indicador": "CUMPRIMENTO_VIAGENS", "valor": 50.0, "unidade": "%",
              "periodo_inicio": D0, "periodo_fim": D0 + M15, "versao_conjunto": "c1",
              "qualidade": {"versao_reconstrucao": "r1"}}
    gravar_indicador(campos, falhas=FalhasColeta(agora=DEPOIS, intervalo=30))
    salvo = IndicadorOperacional.query.one().qualidade
    assert salvo["versao_reconstrucao"] == "r1" and "falhas_coleta" not in campos["qualidade"]
    bloco = salvo["falhas_coleta"]
    assert (bloco["percentual_falhas"], bloco["incompleto"], bloco["escopo"]) == (13.33, True, "COLETA_GLOBAL")
    assert (bloco["inicio"], bloco["fim"]) == (utc(D0), utc(D0 + M15))
    assert bloco["linha_exibida"] == "8000-10" and bloco["versao_regra"] == VERSAO_REGRA_FALHAS


def test_falhas_coleta_consulta_cada_dia_uma_vez(app, monkeypatch):
    import app.qualidade.service as servico
    chamadas = []
    original = servico.consolidar_qualidade
    monkeypatch.setattr(servico, "consolidar_qualidade",
                        lambda *a, **kw: chamadas.append(a) or original(*a, **kw))
    calculo = FalhasColeta(agora=DEPOIS, intervalo=30)
    for k in range(4):
        calculo.da_janela(D0 + k * M15, D0 + (k + 1) * M15)
    assert chamadas == [(DIA, DIA)]


def test_janela_invalida_e_rejeitada(app):
    with pytest.raises(ValueError):
        falhas(D0 + M15, D0)
    with pytest.raises(ValueError):
        falhas(D0.replace(tzinfo=None), (D0 + M15).replace(tzinfo=None))
