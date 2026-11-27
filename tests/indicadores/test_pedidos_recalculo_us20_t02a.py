from datetime import timedelta

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError, OperationalError

from app import create_app, db
from app.models import ExecucaoProcessamento, IndicadorOperacional
from app.reprocessamento.pedidos import (
    TIPO, chave_logica, pedidos_pendentes, registrar_pedidos, registrar_pedidos_do_plano, _item,
)
from conftest import AGORA

JANELA = timedelta(minutes=15)
V = dict(versao_base="base1", versao_regra="regra2", versao_codigo="git1")


def _janela(n=0, indicador="PONTUALIDADE_VIAGENS", gtfs=None, linha="3762-10", sentido="0"):
    inicio = AGORA + n * JANELA
    return {"id": f"r{n}-{indicador}", "indicador": indicador, "inicio": inicio.isoformat(),
            "fim": (inicio + JANELA).isoformat(), "gtfs": gtfs, "linha": linha,
            "sentido": sentido, "parada": None, "trecho": None}


def _registrar(itens, **extra):
    return registrar_pedidos(itens, origem={"tipo": "TESTE"}, **{**V, **extra})


def _contar():
    return ExecucaoProcessamento.query.filter_by(tipo=TIPO).count()


def test_cria_e_recupera_pedido_com_escopo_versoes_e_rastreabilidade(programacao):
    versao, linha, _, _ = programacao
    [pedido] = _registrar([_janela(gtfs=versao.id, linha=linha.codigo)])
    identificador = pedido.id
    db.session.expire_all()
    salvo = db.session.get(ExecucaoProcessamento, identificador)
    assert (salvo.tipo, salvo.estado, salvo.tentativas) == (TIPO, "AGUARDANDO", 0)
    assert (salvo.periodo_inicio, salvo.periodo_fim) == (AGORA, AGORA + JANELA)
    assert (salvo.versao_base, salvo.versao_regra, salvo.versao_codigo) == ("base1", "regra2", "git1")
    assert salvo.versao_gtfs_id == versao.id and salvo.criada_em is not None
    assert salvo.parametros["escopo"] == {"gtfs": versao.id, "linha": linha.codigo, "sentido": "0",
                                          "parada": None, "trecho": None}
    assert salvo.parametros["indicador"] == "PONTUALIDADE_VIAGENS"
    assert salvo.parametros["resultados_ids"] == ["r0-PONTUALIDADE_VIAGENS"]
    assert salvo.parametros["origem"] == {"tipo": "TESTE"}
    assert salvo.chave_logica.startswith("recalculo-us20:") and len(salvo.chave_logica) <= 255


def test_mesma_chave_reutiliza_pedido_sem_duplicar(programacao):
    primeiro = _registrar([_janela()])[0]
    # Pedido repetido com resultado de outra versão na mesma janela/escopo.
    repetido = dict(_janela(), id="outro-resultado")
    assert _registrar([repetido], versao_codigo="git2")[0].id == primeiro.id
    assert _registrar([_janela(), repetido])[0].id == primeiro.id
    assert _contar() == 1
    assert db.session.get(ExecucaoProcessamento, primeiro.id).versao_codigo == "git1"


def test_estado_e_tentativas_persistem_e_pedido_concluido_nao_e_recriado(programacao):
    pedido = _registrar([_janela()])[0]
    pedido.estado, pedido.tentativas, pedido.erro = "ERRO", 2, "TimeoutError"
    db.session.commit()
    db.session.expire_all()
    reaproveitado = _registrar([_janela()])[0]
    assert (reaproveitado.id, reaproveitado.estado, reaproveitado.tentativas,
            reaproveitado.erro) == (pedido.id, "ERRO", 2, "TimeoutError")
    reaproveitado.estado, reaproveitado.finalizada_em = "PRONTO", AGORA
    db.session.commit()
    assert _registrar([_janela()])[0].estado == "PRONTO" and _contar() == 1


@pytest.mark.parametrize("mudanca", [{"versao_regra": "regra3"}, {"versao_base": "base2"}])
def test_nova_versao_de_regra_ou_dados_gera_pedido_distinto(programacao, mudanca):
    antigo = _registrar([_janela()])[0]
    novo = _registrar([_janela()], **mudanca)[0]
    assert novo.id != antigo.id and novo.chave_logica != antigo.chave_logica
    assert _contar() == 2


def test_janelas_indicadores_e_escopos_diferentes_sao_independentes(programacao):
    itens = [_janela(0), _janela(1), _janela(0, indicador="EXCESSO_ESPERA"),
             _janela(0, sentido="1"), _janela(0, linha="8000-10")]
    pedidos = _registrar(itens)
    assert len({p.id for p in pedidos}) == 5 == _contar()
    assert len({chave_logica(_item(i), "base1", "regra2") for i in itens}) == 5


def test_reinicio_simulado_recupera_pedidos_pendentes(tmp_path):
    config = {"TESTING": True, "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'p.db'}",
              "MYSQL_SSL_CA": None}
    primeira = create_app(config)
    with primeira.app_context():
        db.create_all()
        aguardando, em_curso, travado, pronto = (p.id for p in _registrar([_janela(n) for n in range(4)]))
        valores = {em_curso: ("PROCESSANDO", AGORA + timedelta(hours=1)),
                   travado: ("PROCESSANDO", AGORA - timedelta(minutes=1)), pronto: ("PRONTO", None)}
        for identificador, (estado, reserva) in valores.items():
            pedido = db.session.get(ExecucaoProcessamento, identificador)
            pedido.estado, pedido.tentativas = estado, 1
            pedido.reserva_token, pedido.reserva_ate = ("token" if reserva else None), reserva
        db.session.commit()
        db.session.remove()
        db.engine.dispose()  # encerra o processo: nada fica em memória
    with create_app(config).app_context():
        pendentes = pedidos_pendentes(AGORA)
        assert {p.id for p in pendentes} == {aguardando, travado}
        assert db.session.get(ExecucaoProcessamento, travado).tentativas == 1
        assert _registrar([_janela(0)])[0].id == aguardando and _contar() == 4
        db.session.remove()
        db.engine.dispose()


def test_pedido_gravado_por_outra_conexao_e_reutilizado_pela_restricao_unica(tmp_path, monkeypatch):
    """Corrida perdida: o concorrente confirma a chave depois da consulta e antes do INSERT."""
    from app.repositories import jobs as repositorio
    consultas = []
    original = repositorio.obter_por_chave

    def invisivel_na_primeira(chave):  # a consulta prévia ainda não vê o concorrente
        consultas.append(chave)
        return None if len(consultas) <= 2 else original(chave)
    monkeypatch.setattr(repositorio, "obter_por_chave", invisivel_na_primeira)
    uri = f"sqlite:///{tmp_path / 'c.db'}"
    with create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": uri, "MYSQL_SSL_CA": None}).app_context():
        db.create_all()
        chave = chave_logica(_item(_janela()), "base1", "regra2")
        concorrente = create_engine(uri)
        with concorrente.begin() as conexao:
            conexao.execute(text(
                "INSERT INTO execucoes_processamento (id, tipo, chave_logica, parametros, estado,"
                " periodo_inicio, periodo_fim, bruto_id_maximo, versao_base, versao_codigo,"
                " versao_regra, tentativas, criada_em) VALUES ('vencedor', :tipo, :chave, '{}',"
                " 'AGUARDANDO', '2026-11-24 12:00:00', '2026-11-24 12:15:00', 0, 'base1', 'git1',"
                " 'regra2', 0, '2026-11-24 12:00:00')"), {"tipo": TIPO, "chave": chave})
        concorrente.dispose()
        pedidos = _registrar([_janela(), _janela(1)])  # o lote segue para a janela seguinte
        assert len(consultas) == 4  # 1ª tentativa barrada pelo UNIQUE; 2ª reutiliza
        assert pedidos[0].id == "vencedor" and pedidos[1].id != "vencedor"
        assert _contar() == 2
        db.session.remove()
        db.engine.dispose()


def test_falha_na_gravacao_desfaz_o_lote_inteiro(programacao):
    with pytest.raises(IntegrityError):  # GTFS inexistente viola a FK, não a unicidade
        _registrar([_janela(0), _janela(1, gtfs="gtfs-inexistente")])
    assert _contar() == 0
    original = db.session.commit

    def falhar():
        raise OperationalError("COMMIT", {}, Exception("conexão perdida"))
    db.session.commit = falhar
    try:
        with pytest.raises(OperationalError):
            _registrar([_janela(0)])
    finally:
        db.session.commit = original
    assert _contar() == 0
    assert _registrar([_janela(0)])[0].estado == "AGUARDANDO" and _contar() == 1


@pytest.mark.parametrize("ruim", [
    {"versao_regra": " "}, {"versao_base": "x" * 129},
])
def test_rejeita_versoes_invalidas_sem_gravar(programacao, ruim):
    with pytest.raises(ValueError):
        _registrar([_janela()], **ruim)
    assert _contar() == 0


def test_rejeita_janela_sem_fuso_ou_vazia(programacao):
    with pytest.raises(ValueError):
        _registrar([dict(_janela(), inicio=AGORA.replace(tzinfo=None).isoformat())])
    with pytest.raises(ValueError):
        _registrar([dict(_janela(), fim=AGORA.isoformat())])
    with pytest.raises(ValueError):
        _registrar([])


def test_compativel_com_plano_de_regra_da_us20_t01b(programacao):
    from app.reprocessamento.planos import planejar_regra
    versao, linha, _, _ = programacao
    for chave, tipo, regra in [("espera", "ESPERA_ESTIMADA", "antiga"),
                               ("excesso", "EXCESSO_ESPERA", "espera-1.0")]:
        db.session.add(IndicadorOperacional(chave_resultado=chave, versao_gtfs_id=versao.id,
            linha_codigo=linha.codigo, sentido="0", indicador=tipo, unidade="min",
            periodo_inicio=AGORA, periodo_fim=AGORA + JANELA, versao_conjunto="v1",
            versao_calculo=regra))
    db.session.commit()
    plano = planejar_regra(regra_anterior="antiga", inicio=AGORA, fim=AGORA + JANELA,
                           versao_base="base1", versao_regra="espera-1.0", versao_codigo="git1")
    pedidos = registrar_pedidos_do_plano(plano)
    assert {p.versao_regra for p in pedidos} == {"espera-1.0"}
    assert sorted(p.parametros["indicador"] for p in pedidos) == ["ESPERA_ESTIMADA", "EXCESSO_ESPERA"]
    assert all(p.parametros["origem"]["plano_id"] == plano.id for p in pedidos)
    assert all(p.parametros["origem"]["tipo"] == "REGRA" for p in pedidos)
    assert {p.bruto_id_maximo for p in pedidos} == {plano.bruto_id_maximo}
    ids = {r["id"] for r in plano.parametros["resultados"]}
    assert {i for p in pedidos for i in p.parametros["resultados_ids"]} == ids
    assert db.session.get(ExecucaoProcessamento, plano.id).tipo == "PLANO_US20"  # plano preservado
    assert [p.id for p in registrar_pedidos_do_plano(plano)] == [p.id for p in pedidos]
    assert _contar() == 2 and IndicadorOperacional.query.count() == 2


def test_pedido_concluido_nao_engole_dados_novos(programacao):
    """Dados novos depois de o pedido ter lido os anteriores abrem nova geração."""
    pronto = _registrar([_janela()], bruto_id_maximo=10)[0]
    assert _registrar([_janela()], bruto_id_maximo=10)[0].id == pronto.id  # repetição
    pronto.estado = "PRONTO"
    db.session.commit()
    assert _registrar([_janela()], bruto_id_maximo=5)[0].id == pronto.id  # estado já lido
    novo = _registrar([_janela()], bruto_id_maximo=11)[0]
    assert novo.id != pronto.id and novo.chave_logica == f"{pronto.chave_logica}:11"
    assert novo.parametros["chave_base"] == pronto.chave_logica
    # Enquanto a nova geração aguarda, mais dados não criam outra: ela ainda vai ler.
    assert _registrar([_janela()], bruto_id_maximo=12)[0].id == novo.id
    assert db.session.get(ExecucaoProcessamento, pronto.id).estado == "PRONTO"
    assert _contar() == 2


def test_pedido_em_processamento_tambem_abre_geracao(programacao):
    em_curso = _registrar([_janela()], bruto_id_maximo=3)[0]
    em_curso.estado, em_curso.tentativas = "PROCESSANDO", 1
    em_curso.reserva_token, em_curso.reserva_ate = "t", AGORA + JANELA
    db.session.commit()
    assert _registrar([_janela()], bruto_id_maximo=4)[0].id != em_curso.id
    assert _contar() == 2


def test_itens_com_regras_diferentes_viram_pedidos_com_a_regra_de_cada_um(programacao):
    itens = [dict(_janela(), regra="pontualidade-1.0"),
             dict(_janela(indicador="EXCESSO_ESPERA"), regra="espera-1.0")]
    pedidos = _registrar(itens)
    assert [p.versao_regra for p in pedidos] == ["pontualidade-1.0", "espera-1.0"]


def test_restricoes_de_integridade_da_tabela(programacao):
    restricoes = inspect(db.engine).get_unique_constraints("execucoes_processamento")
    assert {"name": "uq_execucoes_processamento_chave_logica",
            "column_names": ["chave_logica"]} in [
        {k: r[k] for k in ("name", "column_names")} for r in restricoes]
    pedido = _registrar([_janela()])[0]
    campos = {c: getattr(pedido, c) for c in (
        "tipo", "chave_logica", "parametros", "periodo_inicio", "periodo_fim", "bruto_id_maximo",
        "versao_base", "versao_codigo", "versao_regra")}
    db.session.add(ExecucaoProcessamento(**campos))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()
    for invalido in ({"estado": "PAUSADO"}, {"tentativas": -1}):
        db.session.add(ExecucaoProcessamento(**{**campos, "chave_logica": "outra", **invalido}))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()
    assert _contar() == 1


def test_migracao_0009_cria_unicidade_e_estados_reutilizados():
    from pathlib import Path
    from alembic.script import ScriptDirectory
    raiz = Path(__file__).resolve().parents[2] / "migrations"
    scripts = ScriptDirectory(str(raiz))
    assert len(scripts.get_heads()) == 1  # nenhuma ramificação nova na cadeia
    fonte = (raiz / "versions" / "0009_execucoes_reprocessamento.py").read_text()
    assert 'sa.UniqueConstraint("chave_logica", name="uq_execucoes_processamento_chave_logica")' in fonte
    assert "estado IN ('AGUARDANDO', 'PROCESSANDO', 'PRONTO', 'ERRO')" in fonte


def test_jobservice_e_generico_e_separa_tipos(programacao):
    from app.jobs import JobService
    _registrar([_janela()])
    campos = dict(tipo="EXTRACAO", chave_logica="extracao:1", parametros={},
                  periodo_inicio=AGORA, periodo_fim=AGORA + JANELA, bruto_id_maximo=0,
                  versao_base="b", versao_regra="r", versao_codigo="c")
    servico = JobService()
    [job] = servico.solicitar([campos])
    assert servico.solicitar([campos])[0].id == job.id
    assert [j.id for j in servico.pendentes("EXTRACAO", AGORA)] == [job.id]
    assert len(pedidos_pendentes(AGORA)) == 1  # só RECALCULO_INDICADOR
    with pytest.raises(ValueError):
        servico.solicitar([])
