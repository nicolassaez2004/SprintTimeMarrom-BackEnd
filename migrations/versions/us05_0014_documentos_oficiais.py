"""Acervo de documentos oficiais com PDF original (US05-T01).

Revision ID: us05_0014
Revises: us09_0013
Create Date: 2026-11-19
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = "us05_0014"
down_revision = "us09_0013"
branch_labels = None
depends_on = None

INSTANTE = sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")


def upgrade():
    op.create_table(
        "documentos_oficiais",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("titulo", sa.String(500), nullable=False),
        sa.Column("tipo", sa.String(64), nullable=False),
        sa.Column("data_documento", sa.Date(), nullable=False),
        sa.Column("orgao", sa.String(255), nullable=False),
        sa.Column("fonte", sa.String(1024), nullable=False),
        sa.Column("descoberto_em", INSTANTE, nullable=False),
        sa.Column("catalogado_em", INSTANTE, nullable=False),
        sa.Column("arquivo_id", sa.String(36), nullable=False),
        sa.ForeignKeyConstraint(
            ["arquivo_id"], ["arquivos_armazenados.id"],
            name="fk_documentos_oficiais_arquivo", ondelete="RESTRICT",
        ),
        sa.CheckConstraint("catalogado_em >= descoberto_em",
                           name="ck_documentos_oficiais_catalogacao_apos_descoberta"),
        mysql_engine="InnoDB",
    )
    op.create_index("ix_documentos_oficiais_arquivo_id", "documentos_oficiais", ["arquivo_id"])


def downgrade():
    raise RuntimeError("Downgrade da us05_0014 apagaria o catálogo de documentos; restaure de um backup.")
