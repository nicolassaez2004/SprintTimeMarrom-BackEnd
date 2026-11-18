"""Separa dados tratados de sua origem bruta imutável (US12-T01B).

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-18

Somente cria a tabela de tratados; não reescreve payloads ou tabelas existentes.
Linha/viagem/indicadores pertencem a histórias posteriores.
"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "observacoes_tratadas",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("bruto_id", sa.BigInteger(), nullable=False),
        sa.Column("valida", sa.Boolean(), nullable=False),
        sa.Column("motivo_invalidez", sa.Text(), nullable=True),
        sa.Column("versao_correspondencia", sa.String(128), nullable=False),
        sa.Column("versao_gtfs_id", sa.String(36), nullable=True),
        sa.ForeignKeyConstraint(["bruto_id"], ["registros_gps.id"],
                                name="fk_observacoes_tratadas_bruto", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["versao_gtfs_id"], ["versoes_gtfs.id"],
                                name="fk_observacoes_tratadas_gtfs", ondelete="RESTRICT"),
        sa.UniqueConstraint("bruto_id", name="uq_observacoes_tratadas_bruto"),
        sa.CheckConstraint("LENGTH(TRIM(versao_correspondencia)) > 0",
                           name="ck_observacoes_tratadas_versao"),
        # Herda o utf8mb4 e a collation da base, como a tabela GTFS existente.
        mysql_engine="InnoDB",
    )
    op.create_index("ix_observacoes_tratadas_versao_gtfs_id",
                    "observacoes_tratadas", ["versao_gtfs_id"])


def downgrade():
    raise RuntimeError("Downgrade da 0004 apagaria dados tratados; restaure de um backup.")
