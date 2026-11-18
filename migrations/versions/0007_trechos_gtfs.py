"""Trechos entre paradas consecutivas por linha, sentido e trajeto (US02-T02B).

Só cria a tabela `trechos`. As chaves estrangeiras compostas impedem trechos
com linha ou paradas inexistentes ou de outra versão GTFS.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-18
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

CODIGO = mysql.VARCHAR(64, collation="utf8mb4_0900_bin")


class LINESTRING(sa.types.UserDefinedType):
    """Coluna espacial nativa; x = longitude, y = latitude."""

    cache_ok = True

    def get_col_spec(self, **kw):
        return "LINESTRING"


def upgrade():
    op.create_table(
        "trechos",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("versao_gtfs_id", sa.String(36), nullable=False),
        sa.Column("linha_codigo", CODIGO, nullable=False),
        sa.Column("sentido", sa.String(8), nullable=False),
        sa.Column("trajeto", CODIGO, nullable=False),
        sa.Column("ordem", sa.Integer(), nullable=False),
        sa.Column("parada_origem_codigo", CODIGO, nullable=False),
        sa.Column("parada_destino_codigo", CODIGO, nullable=False),
        sa.Column("geometria", LINESTRING(), nullable=False),
        sa.ForeignKeyConstraint(
            ["versao_gtfs_id", "linha_codigo"], ["linhas.versao_gtfs_id", "linhas.codigo"],
            name="fk_trechos_linha", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["versao_gtfs_id", "parada_origem_codigo"],
            ["paradas.versao_gtfs_id", "paradas.codigo"],
            name="fk_trechos_parada_origem", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["versao_gtfs_id", "parada_destino_codigo"],
            ["paradas.versao_gtfs_id", "paradas.codigo"],
            name="fk_trechos_parada_destino", ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "versao_gtfs_id", "linha_codigo", "trajeto", "ordem",
            name="uq_trechos_trajeto_ordem",
        ),
        sa.CheckConstraint("ordem >= 1", name="ck_trechos_ordem"),
    )


def downgrade():
    raise RuntimeError(
        "Downgrade da 0005 apagaria os trechos importados; restaure de um backup."
    )
