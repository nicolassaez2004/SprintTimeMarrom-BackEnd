"""Armazena arquivos originais e versões GTFS por hash.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-18
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "arquivos_armazenados",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("chave", sa.String(255), nullable=False, unique=True),
        sa.Column("nome_original", sa.String(255), nullable=False),
        sa.Column("mime_type", sa.String(128), nullable=False),
        sa.Column("tamanho_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("criado_em", mysql.DATETIME(fsp=6), nullable=False),
    )
    op.create_index("ix_arquivos_armazenados_sha256", "arquivos_armazenados", ["sha256"])
    op.create_table(
        "versoes_gtfs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("capturada_em", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("hash", sa.String(64), nullable=False),
        sa.Column("vigencia_inicio", sa.Date(), nullable=False),
        sa.Column("vigencia_fim", sa.Date(), nullable=True),
        sa.Column("arquivo_id", sa.String(36), nullable=False, unique=True),
        sa.ForeignKeyConstraint(
            ["arquivo_id"], ["arquivos_armazenados.id"],
            name="fk_versoes_gtfs_arquivo", ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("hash", name="uq_versoes_gtfs_hash"),
    )


def downgrade():
    raise RuntimeError(
        "Downgrade da 0003 descartaria referências ao ZIP GTFS; restaure de um backup."
    )
