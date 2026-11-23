"""Persistência dos valores publicados para a API de indicadores (US11).

Revision ID: us11_0015
Revises: us05_0014
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = "us11_0015"
down_revision = "us05_0014"
branch_labels = None
depends_on = None


def upgrade():
    instante = sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")
    op.create_table(
        "indicadores_operacionais",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("linha_codigo", sa.String(64), nullable=False),
        sa.Column("indicador", sa.String(64), nullable=False),
        sa.Column("valor", sa.Double(), nullable=False),
        sa.Column("unidade", sa.String(32), nullable=False),
        sa.Column("periodo_inicio", instante, nullable=False),
        sa.Column("periodo_fim", instante, nullable=False),
        sa.Column("versao_conjunto", sa.String(128), nullable=False),
        sa.Column("qualidade", sa.JSON(), nullable=False),
        sa.Column("publicado_em", instante, nullable=False),
        sa.UniqueConstraint(
            "linha_codigo", "indicador", "periodo_inicio", "periodo_fim",
            "versao_conjunto", name="uq_indicador_publicado_periodo_versao",
        ),
        sa.CheckConstraint("periodo_fim > periodo_inicio", name="ck_indicador_periodo"),
        mysql_engine="InnoDB",
    )
    op.create_index(
        "ix_indicadores_linha_indicador_inicio", "indicadores_operacionais",
        ["linha_codigo", "indicador", "periodo_inicio"],
    )
    op.create_index("ix_indicadores_versao", "indicadores_operacionais", ["versao_conjunto"])


def downgrade():
    op.drop_index("ix_indicadores_versao", table_name="indicadores_operacionais")
    op.drop_index("ix_indicadores_linha_indicador_inicio", table_name="indicadores_operacionais")
    op.drop_table("indicadores_operacionais")
