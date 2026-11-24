"""Smoke test of the additive US14 migration on SQLite's batch DDL path."""

import importlib.util
from pathlib import Path

from sqlalchemy import (
    Column, DateTime, Double, ForeignKeyConstraint, Integer, MetaData, String,
    Table, UniqueConstraint, create_engine, inspect,
)
from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_migracao_cria_viagens_e_extende_indicadores():
    metadata = MetaData()
    Table("versoes_gtfs", metadata, Column("id", String(36), primary_key=True))
    Table(
        "linhas", metadata,
        Column("versao_gtfs_id", String(36), primary_key=True),
        Column("codigo", String(64), primary_key=True),
        ForeignKeyConstraint(["versao_gtfs_id"], ["versoes_gtfs.id"]),
    )
    Table(
        "paradas", metadata,
        Column("versao_gtfs_id", String(36), primary_key=True),
        Column("codigo", String(64), primary_key=True),
        ForeignKeyConstraint(["versao_gtfs_id"], ["versoes_gtfs.id"]),
    )
    Table(
        "trechos", metadata,
        Column("id", String(36), primary_key=True),
        Column("versao_gtfs_id", String(36), nullable=False),
        Column("linha_codigo", String(64), nullable=False),
        Column("sentido", String(8), nullable=False),
        Column("trajeto", String(64), nullable=False),
        Column("ordem", Integer, nullable=False),
        Column("parada_origem_codigo", String(64), nullable=False),
        Column("parada_destino_codigo", String(64), nullable=False),
        Column("geometria", String, nullable=False),
        UniqueConstraint("versao_gtfs_id", "linha_codigo", "trajeto", "ordem",
                         name="uq_trechos_trajeto_ordem"),
        ForeignKeyConstraint(["versao_gtfs_id", "linha_codigo"],
                             ["linhas.versao_gtfs_id", "linhas.codigo"]),
        ForeignKeyConstraint(["versao_gtfs_id", "parada_origem_codigo"],
                             ["paradas.versao_gtfs_id", "paradas.codigo"]),
        ForeignKeyConstraint(["versao_gtfs_id", "parada_destino_codigo"],
                             ["paradas.versao_gtfs_id", "paradas.codigo"]),
    )
    Table(
        "indicadores_operacionais", metadata,
        Column("id", String(36), primary_key=True),
        Column("linha_codigo", String(64), nullable=False),
        Column("indicador", String(64), nullable=False),
        Column("valor", Double, nullable=False),
        Column("unidade", String(32), nullable=False),
        Column("periodo_inicio", DateTime, nullable=False),
        Column("periodo_fim", DateTime, nullable=False),
        Column("versao_conjunto", String(128), nullable=False),
        Column("qualidade", String, nullable=False),
        Column("publicado_em", DateTime, nullable=False),
        UniqueConstraint("linha_codigo", "indicador", "periodo_inicio", "periodo_fim",
                         "versao_conjunto", name="uq_indicador_publicado_periodo_versao"),
    )
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE INDEX ix_indicadores_linha_indicador_inicio "
            "ON indicadores_operacionais (linha_codigo, indicador, periodo_inicio)"
        )
        connection.exec_driver_sql(
            "CREATE INDEX ix_indicadores_versao ON indicadores_operacionais (versao_conjunto)"
        )
        context = MigrationContext.configure(connection)
        migration_path = Path(__file__).parents[2] / "migrations/versions/us14_0016_cumprimento_operacional.py"
        spec = importlib.util.spec_from_file_location("us14_0016_cumprimento_operacional", migration_path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        with Operations.context(context):
            migration.upgrade()

    inspector = inspect(engine)
    assert "viagens_observadas" in inspector.get_table_names()
    colunas = {item["name"] for item in inspector.get_columns("indicadores_operacionais")}
    assert {"versao_gtfs_id", "sentido", "parada_codigo", "trecho_id",
            "contagem_numerador", "contagem_denominador", "versao_correspondencia",
            "versao_calculo", "chave_resultado"} <= colunas
    uniques = {item.get("name") for item in inspector.get_unique_constraints("indicadores_operacionais")}
    assert "uq_indicadores_chave_resultado" in uniques
    engine.dispose()
