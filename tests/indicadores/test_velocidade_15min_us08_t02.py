"""US08-T02: velocidade média observada dos ônibus por trecho em janelas de 15 min."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app import db
from app.contexto.trafego import (
    FONTE_AGREGADO, INVALIDA, PENDENTE, SEM_DADOS, VALIDA, AgregadorVelocidade, AmostraVelocidade,
    Limites, agregar_periodo, janelas_15min,
)
from app.core.tempo import FUSO
from app.models import (
    ExecucaoColeta, MedicaoContexto, ObservacaoTratada, Parada, RegistroBrutoGPS, Trecho,
    ViagemObservada,
)
from app.viagens.passagens import estimar_e_gravar, versao_estimativa

UTC = timezone.utc
T0 = datetime(2026, 11, 24, 12, tzinfo=UTC)  # 09:00 em São Paulo


def amostra(segundos, kmh=None, *, trecho="tr-1", situacao=VALIDA, motivo=None, distancia=600.0,
            viagem="vg-1", base=T0, versao_gtfs="v1", sentido="0"):
    """Amostra da US08-T01 com velocidade ``kmh`` sobre ``distancia`` metros."""
    instante = base + timedelta(seconds=segundos)
    duracao = distancia / kmh * 3.6 if kmh else 60.0
    return AmostraVelocidade(
        trecho_id=trecho, versao_gtfs_id=versao_gtfs, linha_codigo="L", sentido=sentido,
        situacao=situacao, motivo=motivo, viagem_observada_id=viagem,
        passagem_origem_id=f"po-{viagem}-{segundos}", passagem_destino_id=f"pd-{viagem}-{segundos}",
        versao_estimativa="us15-t01-v1-lacuna120s", instante_origem=instante,
        instante_destino=instante + timedelta(seconds=duracao), duracao_seg=duracao,
        distancia_m=distancia, velocidade_kmh=kmh,
        versao_calculo="us08-t01-v1-lacuna120-vmin1-vmax80")


def agregar(amostras, metodo="ARITMETICA", inicio=T0, fim=T0 + timedelta(minutes=30), trechos=()):
    return {(j.trecho_id, j.inicio): j for j in AgregadorVelocidade(metodo).agregar(
        amostras, inicio, fim, trechos)}


def test_media_de_velocidades_conhecidas_numa_janela():
    janelas = agregar([amostra(0, 20), amostra(300, 30), amostra(600, 40, viagem="vg-2")])
    j = janelas["tr-1", T0]
    assert j.velocidade_media_kmh == pytest.approx(30.0) and j.motivo is None
    assert j.dados["amostras_validas"] == 3 and j.dados["viagens_validas"] == 2
    # Espacial: 1800 m ÷ (108 + 72 + 54) s × 3,6 = 27,69 km/h (harmônica).
    assert j.dados["candidatos_kmh"] == pytest.approx(
        {"ARITMETICA": 30.0, "ESPACIAL": 1800 / 234 * 3.6})
    espacial = agregar([amostra(0, 20), amostra(300, 30), amostra(600, 40)], metodo="ESPACIAL")
    assert espacial["tr-1", T0].velocidade_media_kmh == pytest.approx(27.6923, rel=1e-4)


def test_metodo_nao_definido_deixa_valor_nulo_com_candidatos():
    j = agregar([amostra(0, 20), amostra(60, 40)], metodo=None)["tr-1", T0]
    assert (j.velocidade_media_kmh, j.motivo) == (None, "metodo_media_indefinido")
    assert j.dados["candidatos_kmh"]["ARITMETICA"] == pytest.approx(30)
    assert j.dados["versoes"]["agregado"] == "us08-t02-v1-mediaINDEF-INSTANTE_ORIGEM"
    with pytest.raises(ValueError):
        AgregadorVelocidade("MEDIANA")


def test_separa_duas_janelas_e_respeita_limites_exatos():
    janelas = agregar([amostra(0, 10), amostra(899.999999, 20), amostra(900, 30), amostra(1799, 50),
                       amostra(1800, 99)])
    assert janelas["tr-1", T0].velocidade_media_kmh == pytest.approx(15)  # [09:00, 09:15)
    segunda = janelas["tr-1", T0 + timedelta(minutes=15)]
    assert segunda.velocidade_media_kmh == pytest.approx(40)  # 09:15 incluso, 09:30 excluso
    assert (segunda.inicio, segunda.fim) == (T0 + timedelta(minutes=15), T0 + timedelta(minutes=30))
    assert len(janelas) == 2


def test_invalidas_pendentes_e_sem_dados_ficam_fora_da_media_mas_contadas():
    janelas = agregar([
        amostra(0, 30), amostra(60, 50),
        amostra(120, 900, situacao=INVALIDA, motivo="velocidade_acima_do_maximo"),
        amostra(180, None, situacao=INVALIDA, motivo="duracao_nula"),
        amostra(240, None, situacao=INVALIDA, motivo="duracao_nula"),
        amostra(300, 70, situacao=PENDENTE, motivo="plausibilidade_indefinida"),
        AmostraVelocidade("tr-1", "v1", "L", "0", SEM_DADOS, "passagem_origem_ausente"),
    ])
    j = janelas["tr-1", T0]
    assert j.velocidade_media_kmh == pytest.approx(40)
    assert (j.dados["amostras_validas"], j.dados["amostras_descartadas"]) == (2, 4)
    assert j.dados["descartes_por_motivo"] == {
        "duracao_nula": 2, "plausibilidade_indefinida": 1, "velocidade_acima_do_maximo": 1}
    assert j.dados["qualidade"]["proporcao_validas"] == pytest.approx(2 / 6)
    assert len(j.dados["amostras"]) == 6  # rastreio de todas as que entraram na janela


def test_janela_sem_validas_ou_sem_amostras_e_nula_com_motivo():
    janelas = agregar([amostra(0, 900, situacao=INVALIDA, motivo="velocidade_acima_do_maximo"),
                       amostra(950, 30, situacao=PENDENTE, motivo="plausibilidade_indefinida")],
                      trechos=["tr-1", "tr-2"])
    assert (janelas["tr-1", T0].velocidade_media_kmh, janelas["tr-1", T0].motivo) == (
        None, "sem_amostras_validas")
    segunda = janelas["tr-1", T0 + timedelta(minutes=15)]
    assert (segunda.velocidade_media_kmh, segunda.motivo) == (None, "amostras_pendentes")
    vazia = janelas["tr-2", T0]
    assert (vazia.velocidade_media_kmh, vazia.motivo, vazia.dados["amostras_validas"]) == (
        None, "sem_amostras", 0)
    assert vazia.dados["candidatos_kmh"] == {} and vazia.dados["distancia_total_m"] is None


def test_nao_mistura_trechos_e_evidencia_repetida_conta_uma_vez():
    repetida = amostra(0, 30)
    janelas = agregar([repetida, repetida, amostra(60, 10, trecho="tr-2")])
    assert janelas["tr-1", T0].dados["amostras_validas"] == 1
    assert janelas["tr-2", T0].velocidade_media_kmh == pytest.approx(10)


def test_meia_noite_de_sao_paulo():
    meia_noite = datetime(2026, 11, 25, tzinfo=FUSO).astimezone(UTC)  # 03:00 UTC
    inicio, fim = meia_noite - timedelta(minutes=15), meia_noite + timedelta(minutes=15)
    janelas = agregar([amostra(-1, 20, base=meia_noite), amostra(0, 40, base=meia_noite)],
                      inicio=inicio, fim=fim)
    antes, depois = janelas["tr-1", inicio], janelas["tr-1", meia_noite]
    assert antes.velocidade_media_kmh == pytest.approx(20)   # 23:59:59 do dia 24
    assert depois.velocidade_media_kmh == pytest.approx(40)  # 00:00:00 do dia 25
    assert depois.inicio.astimezone(FUSO).isoformat() == "2026-11-25T00:00:00-03:00"


@pytest.mark.parametrize("inicio, fim", [
    (T0 + timedelta(minutes=5), T0 + timedelta(minutes=20)),
    (T0, T0),
    (T0.replace(tzinfo=None), T0 + timedelta(minutes=15)),
])
def test_periodo_precisa_ser_alinhado_e_com_fuso(inicio, fim):
    with pytest.raises(ValueError):
        janelas_15min(inicio, fim)


# --- integração: passagens da US15-T01 → amostras da T01 → MedicaoContexto ----

LAT = -23.55
PARADAS = {"A": -46.700, "B": -46.697, "C": -46.694}
LACUNA = timedelta(seconds=120)
VERSAO_EST = versao_estimativa(LACUNA)
LIMITES = Limites(LACUNA, 1.0, 80.0)  # valores de ensaio; os oficiais aguardam o PO


@pytest.fixture
def cenario(programacao):
    versao, linha, importacao, partidas = programacao
    db.session.add_all(Parada(versao_gtfs_id=versao.id, codigo=c, nome=c,
                              latitude=Decimal(str(LAT)), longitude=Decimal(str(lon)))
                       for c, lon in PARADAS.items())
    for i, (o, d) in enumerate((("A", "B"), ("B", "C"))):
        db.session.add(Trecho(versao_gtfs_id=versao.id, linha_codigo=linha.codigo, sentido="0",
                              trajeto="T1", ordem=i + 1, parada_origem_codigo=o,
                              parada_destino_codigo=d,
                              geometria=[[PARADAS[o], LAT], [PARADAS[d], LAT]]))
    execucao = ExecucaoColeta(iniciada_em=T0, estado="OK")
    db.session.add(execucao)
    db.session.commit()
    return versao, linha, partidas, execucao


def viagem(cenario, partida, prefixo, *pontos, reconstrucao="recon-sint"):
    versao, linha, partidas, execucao = cenario
    saida = partidas[partida].saida_prevista
    v = ViagemObservada(partida_programada_id=partidas[partida].id, saida_observada=saida,
                        cobertura_tracado=1, versao_reconstrucao=reconstrucao,
                        versao_correspondencia="match-sint")
    db.session.add(v)
    obs = []
    for segundos, parada in pontos:
        bruto = RegistroBrutoGPS(execucao=execucao, prefixo_origem=prefixo, linha_origem="3762-10",
                                 latitude=LAT, longitude=PARADAS[parada],
                                 horario_gps=saida + timedelta(seconds=segundos))
        db.session.add(bruto)
        obs.append(ObservacaoTratada(bruto=bruto, valida=True, versao_correspondencia="match-sint",
                                     versao_gtfs_id=versao.id, linha_codigo=linha.codigo,
                                     sentido_gtfs="0"))
        db.session.add(obs[-1])
    db.session.flush()
    estimar_e_gravar(v, obs, lacuna_maxima=LACUNA)
    db.session.commit()
    return v


def agregar_banco(cenario, **extra):
    versao, linha, _, _ = cenario
    parametros = dict(versao_gtfs_id=versao.id, linha_codigo=linha.codigo, sentido="0",
                      inicio=T0, fim=T0 + timedelta(minutes=45), versao_estimativa=VERSAO_EST,
                      versao_reconstrucao="recon-sint", versao_correspondencia="match-sint",
                      limites=LIMITES, metodo="ARITMETICA", agora=T0 + timedelta(hours=2))
    resultado = agregar_periodo(**{**parametros, **extra})
    db.session.commit()
    return resultado


def test_persiste_em_medicao_contexto_com_fonte_unidade_qualidade_e_versoes(cenario):
    v1 = viagem(cenario, 0, "5810", (0, "A"), (30, "B"), (90, "C"))
    viagem(cenario, 1, "5811", (0, "A"), (60, "B"))  # partida 12:15 → 2ª janela
    resultado = agregar_banco(cenario)
    ab, bc = Trecho.query.order_by(Trecho.ordem).all()
    assert len(resultado) == 6  # 2 trechos × 3 janelas, inclusive as sem amostra
    registros = MedicaoContexto.query.order_by(MedicaoContexto.inicio, MedicaoContexto.trecho_id).all()
    assert {(r.trecho_id, r.inicio) for r in registros} == {
        (ab.id, T0), (bc.id, T0), (ab.id, T0 + timedelta(minutes=15))}  # vazias não são gravadas
    r = next(x for x in registros if (x.trecho_id, x.inicio) == (ab.id, T0))
    assert (r.tipo, r.fonte, r.fim) == ("TRAFEGO", FONTE_AGREGADO, T0 + timedelta(minutes=15))
    assert r.fonte == "VELOCIDADE_OBSERVADA_ONIBUS" and "não é a velocidade" in r.dados["descricao_fonte"]
    assert r.unidades["velocidade_media_kmh"] == "km/h"
    assert r.dados["velocidade_media_kmh"] == pytest.approx(36.7558, rel=1e-4)
    assert r.versao_fonte == "us08-t02-v1-mediaARITMETICA-INSTANTE_ORIGEM"
    assert r.dados["versoes"] == {
        "gtfs": cenario[0].id, "estimativa": VERSAO_EST, "reconstrucao": "recon-sint",
        "correspondencia": "match-sint", "agregado": r.versao_fonte,
        "calculo_amostras": ["us08-t01-v1-lacuna120-vmin1-vmax80"]}
    assert r.dados["amostras"][0][0] == v1.id and r.dados["amostras"][0][3] == VALIDA
    falhas = r.dados["qualidade"]["falhas_coleta"]
    assert falhas["inicio"] == T0.isoformat() and "situacao" in falhas
    vazia = next(j for j in resultado if j.trecho_id == bc.id and j.inicio == T0 + timedelta(minutes=30))
    assert (vazia.velocidade_media_kmh, vazia.motivo) == (None, "sem_amostras")


def test_reprocessamento_e_deterministico_e_nao_duplica(cenario):
    viagem(cenario, 0, "5810", (0, "A"), (30, "B"), (90, "C"))
    agregar_banco(cenario)
    antes = {r.chave: (r.id, r.dados, r.coletada_em) for r in MedicaoContexto.query}
    agregar_banco(cenario)
    depois = {r.chave: (r.id, r.dados, r.coletada_em) for r in MedicaoContexto.query}
    assert antes == depois and len(depois) == 2
    # Outro método é outra versão: grava ao lado, sem sobrescrever.
    agregar_banco(cenario, metodo="ESPACIAL")
    assert MedicaoContexto.query.count() == 4


def test_reprocessamento_remove_janela_que_nao_se_reproduz(cenario):
    viagem(cenario, 0, "5810", (0, "A"), (30, "B"), (90, "C"))
    agregar_banco(cenario)
    trecho_bc = Trecho.query.filter_by(ordem=2).one()
    trecho_bc.geometria = [[PARADAS["B"], LAT], [PARADAS["B"], LAT]]  # B→C passa a ser inválida
    db.session.commit()
    agregar_banco(cenario)
    bc = MedicaoContexto.query.filter_by(trecho_id=trecho_bc.id).one()
    assert (bc.dados["velocidade_media_kmh"], bc.dados["motivo"]) == (None, "sem_amostras_validas")
    assert bc.dados["descartes_por_motivo"] == {"distancia_invalida": 1}
    assert MedicaoContexto.query.count() == 2


def test_nao_mistura_versoes_de_reconstrucao_nem_sentidos(cenario):
    viagem(cenario, 0, "5810", (0, "A"), (30, "B"))
    viagem(cenario, 1, "5811", (0, "A"), (60, "B"), reconstrucao="recon-outra")
    agregar_banco(cenario)
    assert MedicaoContexto.query.count() == 1
    agregar_banco(cenario, versao_reconstrucao="recon-outra")
    registros = MedicaoContexto.query.all()
    assert len(registros) == 2
    assert {r.dados["versoes"]["reconstrucao"] for r in registros} == {"recon-sint", "recon-outra"}
    # Sentido 1 não tem trechos nem viagens: nada é atribuído ao sentido 0.
    assert all(j.motivo == "sem_amostras" for j in agregar_banco(cenario, sentido="1"))
    assert MedicaoContexto.query.count() == 2


def test_sem_limites_de_plausibilidade_nada_vira_media(cenario):
    viagem(cenario, 0, "5810", (0, "A"), (30, "B"))
    janela = next(j for j in agregar_banco(cenario, limites=Limites(LACUNA)) if j.motivo != "sem_amostras")
    assert (janela.velocidade_media_kmh, janela.motivo) == (None, "amostras_pendentes")
    assert MedicaoContexto.query.one().dados["amostras_descartadas"] == 1


def test_migracao_us08_0024_corresponde_ao_modelo_e_compila_para_mysql(app):
    import importlib.util
    import io
    from pathlib import Path

    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine

    arquivo = Path(__file__).resolve().parents[2] / "migrations/versions/us08_0024_medicoes_contexto.py"
    spec = importlib.util.spec_from_file_location("migracao_us08", arquivo)
    migracao = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migracao)
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as conexao:
            db.metadata.create_all(conexao, tables=[
                t for t in db.metadata.sorted_tables if t.name != "medicoes_contexto"])
            contexto = MigrationContext.configure(conexao)
            with Operations.context(contexto):
                migracao.upgrade()
            assert compare_metadata(contexto, db.metadata) == []
    finally:
        engine.dispose()
    saida = io.StringIO()
    offline = MigrationContext.configure(dialect_name="mysql", opts={"as_sql": True, "output_buffer": saida})
    with Operations.context(offline):
        migracao.upgrade()
    sql = saida.getvalue()
    assert "CREATE TABLE medicoes_contexto" in sql and "DATETIME(6)" in sql
    assert "REFERENCES trechos (id)" in sql and "ENGINE=InnoDB" in sql
