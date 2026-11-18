"""Linhas e paradas importadas de uma versão GTFS (US02-T02A).

Só cria tabelas novas. A chave (versao_gtfs_id, codigo) mantém códigos
repetidos entre versões e cada registro ligado à versão de origem.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-18
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

CODIGO = mysql.VARCHAR(64, collation="utf8mb4_0900_bin")


def _fk_versao(tabela):
    return sa.ForeignKeyConstraint(
        ["versao_gtfs_id"], ["versoes_gtfs.id"],
        name=f"fk_{tabela}_versao_gtfs", ondelete="RESTRICT",
    )


def upgrade():
    op.create_table(
        "linhas",
        sa.Column("versao_gtfs_id", sa.String(36), nullable=False),
        sa.Column("codigo", CODIGO, nullable=False),
        sa.Column("nome", sa.String(255), nullable=False),
        sa.PrimaryKeyConstraint("versao_gtfs_id", "codigo"),
        _fk_versao("linhas"),
    )
    op.create_table(
        "paradas",
        sa.Column("versao_gtfs_id", sa.String(36), nullable=False),
        sa.Column("codigo", CODIGO, nullable=False),
        sa.Column("nome", sa.String(255), nullable=False),
        sa.Column("latitude", sa.Numeric(11, 8), nullable=False),
        sa.Column("longitude", sa.Numeric(11, 8), nullable=False),
        sa.PrimaryKeyConstraint("versao_gtfs_id", "codigo"),
        sa.CheckConstraint("latitude BETWEEN -90 AND 90", name="ck_paradas_latitude"),
        sa.CheckConstraint("longitude BETWEEN -180 AND 180", name="ck_paradas_longitude"),
        _fk_versao("paradas"),
    )


def downgrade():
    raise RuntimeError(
        "Downgrade da 0004 apagaria linhas e paradas importadas; restaure de um backup."
    )
