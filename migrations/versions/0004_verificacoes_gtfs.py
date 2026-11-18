"""Histórico da agenda e do prazo de incorporação de versões GTFS.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-18
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    instante = mysql.DATETIME(fsp=6)
    op.create_table(
        "verificacoes_gtfs",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
                  primary_key=True, autoincrement=True),
        sa.Column("prevista_em", instante, nullable=False),
        sa.Column("iniciada_em", instante, nullable=False),
        sa.Column("finalizada_em", instante, nullable=True),
        sa.Column("estado", sa.String(16), nullable=False),
        sa.Column("resultado", sa.String(16), nullable=True),
        sa.Column("hash_identificado", sa.String(64), nullable=True),
        sa.Column("identificada_em", instante, nullable=True),
        sa.Column("persistida_em", instante, nullable=True),
        sa.Column("versao_id", sa.String(36), nullable=True),
        sa.Column("erro", sa.String(128), nullable=True),
        sa.CheckConstraint("estado IN ('EM_ANDAMENTO', 'OK', 'PARCIAL', 'FALHA')",
                           name="ck_verificacoes_gtfs_estado"),
        sa.ForeignKeyConstraint(["versao_id"], ["versoes_gtfs.id"],
                                name="fk_verificacoes_gtfs_versao", ondelete="RESTRICT"),
    )
    op.create_index("ix_verificacoes_gtfs_iniciada_em", "verificacoes_gtfs", ["iniciada_em"])
    op.create_index("ix_verificacoes_gtfs_hash_identificado", "verificacoes_gtfs", ["hash_identificado"])


def downgrade():
    raise RuntimeError("Downgrade da 0004 apagaria o histórico GTFS; restaure de um backup.")
