"""Manifesta execuções reproduzíveis e associa tratados a cada execução.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-19

Não altera nem apaga registros GPS. Observações históricas mantêm execução NULL.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

DATETIME6 = mysql.DATETIME(fsp=6)


def upgrade():
    op.create_table(
        "execucoes_processamento",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tipo", sa.String(32), nullable=False),
        sa.Column("periodo_inicio", DATETIME6, nullable=False),
        sa.Column("periodo_fim", DATETIME6, nullable=False),
        sa.Column("bruto_id_maximo", sa.BigInteger(), nullable=False),
        sa.Column("versao_base", sa.String(128), nullable=False),
        sa.Column("versao_gtfs_id", sa.String(36), nullable=True),
        sa.Column("versao_codigo", sa.String(128), nullable=False),
        sa.Column("versao_regra", sa.String(128), nullable=False),
        sa.Column("estado", sa.String(16), nullable=False),
        sa.Column("tentativas", sa.Integer(), nullable=False),
        sa.Column("criada_em", DATETIME6, nullable=False),
        sa.Column("finalizada_em", DATETIME6, nullable=True),
        sa.Column("erro", sa.String(128), nullable=True),
        sa.ForeignKeyConstraint(
            ["versao_gtfs_id"], ["versoes_gtfs.id"],
            name="fk_execucoes_processamento_gtfs", ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "estado IN ('EM_ANDAMENTO', 'CONCLUIDA', 'FALHA')",
            name="ck_execucoes_processamento_estado",
        ),
        sa.CheckConstraint("periodo_fim > periodo_inicio",
                           name="ck_execucoes_processamento_periodo"),
        sa.CheckConstraint("tentativas >= 0",
                           name="ck_execucoes_processamento_tentativas"),
        sa.Index("ix_execucoes_processamento_periodo", "periodo_inicio", "periodo_fim"),
        mysql_engine="InnoDB",
    )

    op.drop_constraint(
        "uq_observacoes_tratadas_bruto", "observacoes_tratadas", type_="unique"
    )
    op.add_column(
        "observacoes_tratadas",
        sa.Column("execucao_processamento_id", sa.String(36), nullable=True),
    )
    op.create_foreign_key(
        "fk_observacoes_execucao", "observacoes_tratadas", "execucoes_processamento",
        ["execucao_processamento_id"], ["id"], ondelete="RESTRICT",
    )
    op.create_index(
        "ix_observacoes_tratadas_execucao_processamento_id",
        "observacoes_tratadas", ["execucao_processamento_id"],
    )
    op.create_unique_constraint(
        "uq_observacoes_execucao_bruto", "observacoes_tratadas",
        ["execucao_processamento_id", "bruto_id"],
    )


def downgrade():
    raise RuntimeError(
        "Downgrade da 0008 eliminaria execuções versionadas; restaure de um backup."
    )
