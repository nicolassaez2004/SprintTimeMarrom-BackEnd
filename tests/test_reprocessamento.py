from copy import deepcopy
from datetime import datetime, timezone

from app import create_app, db
from app.models import ExecucaoColeta, ExecucaoProcessamento, ObservacaoTratada, RegistroBrutoGPS
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
            ).id
            for _ in range(2)
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
                    manifesto.versao_regra, manifesto.estado) == (
                "gps:id<=2", "git:abc123", "validacao-v1", "CONCLUIDA"
            )
            resultados.append([
                (o.bruto_id, o.valida, o.motivo_invalidez,
                 o.versao_correspondencia, o.versao_gtfs_id)
                for o in ObservacaoTratada.query.filter_by(
                    execucao_processamento_id=execucao_id
                ).order_by(ObservacaoTratada.bruto_id)
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

        db.session.remove()
        db.engine.dispose()
