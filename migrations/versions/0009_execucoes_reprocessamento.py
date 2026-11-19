"""Manifesta execuções reproduzíveis e guarda seus resultados por bruto.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-19

Não altera nem apaga registros GPS nem muda a cardinalidade de ObservacaoTratada.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

DATETIME6 = mysql.DATETIME(fsp=6)


def upgrade():
    op.create_table(
        "execucoes_processamento",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tipo", sa.String(32), nullable=False),
        sa.Column("chave_logica", sa.String(255), nullable=False),
        sa.Column("parametros", sa.JSON(), nullable=False),
        sa.Column("estado", sa.String(16), nullable=False),
        sa.Column("periodo_inicio", DATETIME6, nullable=False),
        sa.Column("periodo_fim", DATETIME6, nullable=False),
        sa.Column("bruto_id_maximo", sa.BigInteger(), nullable=False),
        sa.Column("versao_base", sa.String(128), nullable=False),
        sa.Column("versao_gtfs_id", sa.String(36), nullable=True),
        sa.Column("versao_codigo", sa.String(128), nullable=False),
        sa.Column("versao_regra", sa.String(128), nullable=False),
        sa.Column("tentativas", sa.Integer(), nullable=False),
        sa.Column("criada_em", DATETIME6, nullable=False),
        sa.Column("iniciada_em", DATETIME6, nullable=True),
        sa.Column("finalizada_em", DATETIME6, nullable=True),
        sa.Column("reserva_token", sa.String(36), nullable=True),
        sa.Column("reserva_ate", DATETIME6, nullable=True),
        sa.Column("erro", sa.String(128), nullable=True),
        sa.ForeignKeyConstraint(
            ["versao_gtfs_id"], ["versoes_gtfs.id"],
            name="fk_execucoes_processamento_gtfs", ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "estado IN ('AGUARDANDO', 'PROCESSANDO', 'PRONTO', 'ERRO')",
            name="ck_execucoes_processamento_estado",
        ),
        sa.CheckConstraint("periodo_fim > periodo_inicio",
                           name="ck_execucoes_processamento_periodo"),
        sa.CheckConstraint("tentativas >= 0",
                           name="ck_execucoes_processamento_tentativas"),
        sa.CheckConstraint(
            "(reserva_token IS NULL AND reserva_ate IS NULL) OR "
            "(reserva_token IS NOT NULL AND reserva_ate IS NOT NULL)",
            name="ck_execucoes_processamento_reserva",
        ),
        sa.UniqueConstraint("chave_logica", name="uq_execucoes_processamento_chave_logica"),
        sa.Index("ix_execucoes_processamento_periodo", "periodo_inicio", "periodo_fim"),
        mysql_engine="InnoDB",
    )

    op.create_table(
        "resultados_reprocessamento",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("execucao_processamento_id", sa.String(36), nullable=False),
        sa.Column("bruto_id", sa.BigInteger(), nullable=False),
        sa.Column("valida", sa.Boolean(), nullable=False),
        sa.Column("motivo_invalidez", sa.Text(), nullable=True),
        sa.Column("versao_correspondencia", sa.String(128), nullable=False),
        sa.Column("versao_gtfs_id", sa.String(36), nullable=True),
        sa.ForeignKeyConstraint(
            ["execucao_processamento_id"], ["execucoes_processamento.id"],
            name="fk_resultados_reprocessamento_execucao", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["bruto_id"], ["registros_gps.id"],
            name="fk_resultados_reprocessamento_bruto", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["versao_gtfs_id"], ["versoes_gtfs.id"],
            name="fk_resultados_reprocessamento_gtfs", ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "execucao_processamento_id", "bruto_id",
            name="uq_resultados_reprocessamento_execucao_bruto",
        ),
        sa.CheckConstraint(
            "LENGTH(TRIM(versao_correspondencia)) > 0",
            name="ck_resultados_reprocessamento_versao",
        ),
        mysql_engine="InnoDB",
    )
    op.create_index("ix_resultados_reprocessamento_execucao_processamento_id",
                    "resultados_reprocessamento", ["execucao_processamento_id"])
    op.create_index("ix_resultados_reprocessamento_bruto_id",
                    "resultados_reprocessamento", ["bruto_id"])


def downgrade():
    raise RuntimeError(
        "Downgrade da 0009 eliminaria execuções versionadas; restaure de um backup."
    )
