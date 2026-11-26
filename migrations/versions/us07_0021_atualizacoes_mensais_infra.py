"""Track monthly infrastructure refresh attempts (US07).

Revision ID: us07_0021
Revises: us07_0020
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = "us07_0021"
down_revision = "us07_0020"
branch_labels = None
depends_on = None

INSTANTE = sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")


def upgrade():
    op.create_table(
        "atualizacoes_mensais_infraestrutura",
        sa.Column("mes", sa.String(7), primary_key=True),
        sa.Column("iniciada_em", INSTANTE, nullable=False),
        sa.Column("finalizada_em", INSTANTE, nullable=True),
        sa.Column("estado", sa.String(16), nullable=False),
        sa.Column("resultado", sa.JSON(), nullable=False),
        sa.CheckConstraint("estado IN ('EM_ANDAMENTO', 'OK', 'FALHA')",
                           name="ck_atualizacao_mensal_infra_estado"),
        mysql_engine="InnoDB",
    )


def downgrade():
    op.drop_table("atualizacoes_mensais_infraestrutura")
