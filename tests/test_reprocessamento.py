from copy import deepcopy
from datetime import datetime, timedelta, timezone

from app import create_app, db
from app.models import (
    ExecucaoColeta,
    ExecucaoProcessamento,
    ObservacaoTratada,
    RegistroBrutoGPS,
    ResultadoReprocessamento,
)
from app.persistencia.reprocessamento import (
    criar_execucao_reprocessamento,
    executar_reprocessamento,
)


def test_manifesto_fixa_conjunto_versoes_e_resultados_repetiveis():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://"})
    inicio = datetime(2026, 11, 18, tzinfo=timezone.utc)
    fim = datetime(2026, 11, 19, tzinfo=timezone.utc)
    with app.app_context():
        db.create_all()
        coleta = ExecucaoColeta(estado="OK", novos_persistidos=2)
        db.session.add(coleta)
        db.session.flush()
        brutos = [
            RegistroBrutoGPS(
                execucao_id=coleta.id, prefixo_origem=str(i), latitude=-23.5,
                longitude=-46.6, horario_gps=inicio, coletado_em=inicio,
                payload={"p": i},
            )
            for i in (5810, 5811)
        ]
        db.session.add_all(brutos)
        db.session.commit()
        snapshot_origem = [
            (bruto.id, deepcopy(bruto.payload), bruto.latitude, bruto.longitude,
             bruto.horario_gps, bruto.coletado_em)
            for bruto in RegistroBrutoGPS.query.order_by(RegistroBrutoGPS.id)
        ]

        execucoes = [
            criar_execucao_reprocessamento(
                periodo_inicio=inicio, periodo_fim=fim, versao_base="gps:id<=2",
                versao_codigo="git:abc123", versao_regra="validacao-v1",
                chave_logica=f"reprocessamento-replay-{indice}",
            ).id
            for indice in range(2)
        ]
        bruto_tardio = RegistroBrutoGPS(
            execucao_id=coleta.id, prefixo_origem="9999", latitude=-23.5,
            longitude=-46.6, horario_gps=inicio, coletado_em=inicio,
            payload={"p": 9999},
        )
        db.session.add(bruto_tardio)
        db.session.commit()
        snapshot_origem = [
            (bruto.id, deepcopy(bruto.payload), bruto.latitude, bruto.longitude,
             bruto.horario_gps, bruto.coletado_em)
            for bruto in RegistroBrutoGPS.query.order_by(RegistroBrutoGPS.id)
        ]

        for execucao_id in execucoes:
            assert executar_reprocessamento(execucao_id, tamanho_lote=1) == 2
            # Reexecutar o mesmo manifesto é idempotente e mantém seu conjunto.
            assert executar_reprocessamento(execucao_id, tamanho_lote=1) == 2

        resultados = []
        for execucao_id in execucoes:
            manifesto = db.session.get(ExecucaoProcessamento, execucao_id)
            assert (manifesto.versao_base, manifesto.versao_codigo,
                    manifesto.versao_regra, manifesto.estado,
                    manifesto.chave_logica) == (
                "gps:id<=2", "git:abc123", "validacao-v1", "PRONTO",
                f"reprocessamento-replay-{execucoes.index(execucao_id)}",
            )
            assert manifesto.parametros["snapshot"]["bruto_id_maximo"] == 2
            assert manifesto.reserva_token is None and manifesto.reserva_ate is None
            resultados.append([
                (o.bruto_id, o.valida, o.motivo_invalidez,
                 o.versao_correspondencia, o.versao_gtfs_id)
                for o in ResultadoReprocessamento.query.filter_by(
                    execucao_processamento_id=execucao_id
                ).order_by(ResultadoReprocessamento.bruto_id)
            ])
        assert resultados[0] == resultados[1]
        assert all(
            bruto_tardio.id != resultado[0]
            for grupo in resultados for resultado in grupo
        )
        assert [
            (bruto.id, bruto.payload, bruto.latitude, bruto.longitude,
             bruto.horario_gps, bruto.coletado_em)
            for bruto in RegistroBrutoGPS.query.order_by(RegistroBrutoGPS.id)
        ] == snapshot_origem
        # A visão atual pertence ao MatchingService (US03); o replay só gera snapshots.
        assert ObservacaoTratada.query.count() == 0

        db.session.remove()
        db.engine.dispose()


def test_chave_logica_retorna_manifesto_existente_sem_mudar_versoes():
    from app.persistencia.reprocessamento import (
        ReprocessamentoInvalido,
    )
    import pytest

    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://"})
    inicio = datetime(2026, 11, 18, tzinfo=timezone.utc)
    fim = datetime(2026, 11, 19, tzinfo=timezone.utc)
    with app.app_context():
        db.create_all()
        argumentos = dict(
            periodo_inicio=inicio, periodo_fim=fim, versao_base="base-v1",
            versao_codigo="codigo-v1", versao_regra="regra-v1",
            chave_logica="reprocessamento-chave-1",
        )
        primeiro = criar_execucao_reprocessamento(**argumentos)
        repetido = criar_execucao_reprocessamento(**argumentos)
        assert repetido.id == primeiro.id
        with pytest.raises(ReprocessamentoInvalido, match="chave_logica"):
            criar_execucao_reprocessamento(**{**argumentos, "versao_regra": "regra-v2"})
        db.session.remove()
        db.engine.dispose()


def test_reserva_ativa_impede_segundo_worker():
    import pytest

    from app.persistencia.reprocessamento import ReprocessamentoOcupado

    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://"})
    inicio = datetime(2026, 11, 18, tzinfo=timezone.utc)
    with app.app_context():
        db.create_all()
        manifesto = ExecucaoProcessamento(
            chave_logica="manifesto-reservado",
            periodo_inicio=inicio, periodo_fim=inicio + timedelta(days=1),
            bruto_id_maximo=0, versao_base="base-v1", versao_codigo="codigo-v1",
            versao_regra="regra-v1", estado="PROCESSANDO", tentativas=1,
            reserva_token="worker-1",
            reserva_ate=datetime.now(timezone.utc) + timedelta(days=1),
        )
        db.session.add(manifesto)
        db.session.commit()
        with pytest.raises(ReprocessamentoOcupado, match="reservado"):
            executar_reprocessamento(manifesto.id)
        db.session.refresh(manifesto)
        assert manifesto.estado == "PROCESSANDO"
        assert manifesto.reserva_token == "worker-1"
        db.session.remove()
        db.engine.dispose()


def test_falha_de_lote_registra_erro_e_permite_retoma():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://"})
    inicio = datetime(2026, 11, 18, tzinfo=timezone.utc)
    with app.app_context():
        db.create_all()
        coleta = ExecucaoColeta(estado="OK", novos_persistidos=1)
        db.session.add(coleta)
        db.session.flush()
        db.session.add(RegistroBrutoGPS(
            execucao_id=coleta.id, prefixo_origem="5810", latitude=-23.5,
            longitude=-46.6, horario_gps=inicio, coletado_em=inicio,
            payload={"p": "5810"},
        ))
        db.session.commit()
        manifesto = criar_execucao_reprocessamento(
            periodo_inicio=inicio, periodo_fim=inicio + timedelta(days=1),
            versao_base="base-v1", versao_codigo="codigo-v1",
            versao_regra="regra-v1",
        )

        def falhar(_bruto, _manifesto):
            raise RuntimeError("erro proposital no processamento")

        import pytest
        with pytest.raises(RuntimeError, match="proposital"):
            executar_reprocessamento(manifesto.id, processador=falhar)
        db.session.refresh(manifesto)
        assert manifesto.estado == "ERRO"
        assert manifesto.erro == "RuntimeError"
        assert manifesto.reserva_token is None
        assert ResultadoReprocessamento.query.count() == 0

        assert executar_reprocessamento(manifesto.id) == 1
        db.session.refresh(manifesto)
        assert manifesto.estado == "PRONTO"
        assert ResultadoReprocessamento.query.count() == 1
        assert ObservacaoTratada.query.count() == 0
        db.session.remove()
        db.engine.dispose()


def _brutos(quantidade, instante):
    coleta = ExecucaoColeta(estado="OK", novos_persistidos=quantidade)
    db.session.add(coleta)
    db.session.flush()
    brutos = [
        RegistroBrutoGPS(
            execucao_id=coleta.id, prefixo_origem=str(1000 + i),
            latitude=None if i % 3 == 0 else -23.5, longitude=-46.6,
            horario_gps=instante, coletado_em=instante, payload={"p": i},
        )
        for i in range(quantidade)
    ]
    db.session.add_all(brutos)
    db.session.commit()
    return brutos


def test_lotes_seguem_ordem_de_id_e_resultado_independe_do_tamanho_do_lote():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://"})
    inicio = datetime(2026, 11, 18, tzinfo=timezone.utc)
    with app.app_context():
        db.create_all()
        _brutos(7, inicio)
        vistos = []

        def registrar(bruto, manifesto):
            vistos.append(bruto.id)
            from app.persistencia.reprocessamento import tratar_bruto_basico
            return tratar_bruto_basico(bruto, manifesto)

        resultados = []
        for indice, lote in enumerate((1, 3, 500)):
            vistos.clear()
            execucao = criar_execucao_reprocessamento(
                periodo_inicio=inicio, periodo_fim=inicio + timedelta(days=1),
                versao_base="gps:id<=7", versao_codigo="git:abc", versao_regra="regra-v1",
                chave_logica=f"lote-{indice}",
            )
            assert executar_reprocessamento(execucao.id, processador=registrar,
                                            tamanho_lote=lote) == 7
            assert vistos == sorted(vistos)
            resultados.append([
                (r.bruto_id, r.valida, r.motivo_invalidez, r.versao_correspondencia)
                for r in ResultadoReprocessamento.query.filter_by(
                    execucao_processamento_id=execucao.id
                ).order_by(ResultadoReprocessamento.bruto_id)
            ])
        assert resultados[0] == resultados[1] == resultados[2]
        assert ResultadoReprocessamento.query.count() == 21
        db.session.remove()
        db.engine.dispose()


def test_reprocessamento_nao_sobrescreve_observacao_da_correspondencia_us03():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://"})
    inicio = datetime(2026, 11, 18, tzinfo=timezone.utc)
    with app.app_context():
        db.create_all()
        (bruto,) = _brutos(2, inicio)[1:2]
        db.session.add(ObservacaoTratada(
            bruto_id=bruto.id, valida=False, motivo_invalidez="DISTANCIA_MAIOR_100M",
            motivo_correspondencia="DISTANCIA_MAIOR_100M", versao_correspondencia="tabela-v1",
            linha_codigo="3762-10", sentido_gtfs="0", distancia_tracado_m=250.0,
        ))
        db.session.commit()
        antes = [(o.bruto_id, o.valida, o.motivo_invalidez, o.versao_correspondencia,
                  o.linha_codigo, o.distancia_tracado_m) for o in ObservacaoTratada.query]
        execucao = criar_execucao_reprocessamento(
            periodo_inicio=inicio, periodo_fim=inicio + timedelta(days=1),
            versao_base="b", versao_codigo="c", versao_regra="r",
        )
        assert executar_reprocessamento(execucao.id) == 2
        depois = [(o.bruto_id, o.valida, o.motivo_invalidez, o.versao_correspondencia,
                   o.linha_codigo, o.distancia_tracado_m) for o in ObservacaoTratada.query]
        assert depois == antes
        # O snapshot da execução continua rastreável ao bruto e às versões.
        snapshot = ResultadoReprocessamento.query.filter_by(bruto_id=bruto.id).one()
        assert (snapshot.execucao_processamento_id, snapshot.versao_correspondencia) == (
            execucao.id, "r")
        db.session.remove()
        db.engine.dispose()


def test_validade_pendente_nao_e_convertida_em_invalida():
    import pytest

    from app.persistencia.reprocessamento import ReprocessamentoInvalido

    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://"})
    inicio = datetime(2026, 11, 18, tzinfo=timezone.utc)
    with app.app_context():
        db.create_all()
        _brutos(1, inicio)
        execucao = criar_execucao_reprocessamento(
            periodo_inicio=inicio, periodo_fim=inicio + timedelta(days=1),
            versao_base="b", versao_codigo="c", versao_regra="r",
        )
        with pytest.raises(ReprocessamentoInvalido, match="booleana"):
            executar_reprocessamento(execucao.id,
                                     processador=lambda _b, _m: {"valida": None})
        db.session.refresh(execucao)
        assert execucao.estado == "ERRO"
        assert ResultadoReprocessamento.query.count() == 0
        db.session.remove()
        db.engine.dispose()
