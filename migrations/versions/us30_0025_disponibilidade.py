"""US30: medições externas de disponibilidade.

Revision ID: us30_0025
Revises: us08_0024
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = "us30_0025"
down_revision = "us08_0024"
branch_labels = None
depends_on = None


def upgrade():
    instante = sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")
    op.create_table("verificacoes_disponibilidade",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("alvo", sa.String(16), nullable=False),
        sa.Column("instante", instante, nullable=False),
        sa.Column("duracao_ms", sa.Integer(), nullable=False),
        sa.Column("sucesso", sa.Boolean(), nullable=False),
        sa.Column("status_http", sa.Integer(), nullable=True),
        sa.Column("resultado", sa.String(32), nullable=False),
        sa.CheckConstraint("alvo IN ('API', 'PAINEL')", name="ck_verificacao_disponibilidade_alvo"),
        sa.CheckConstraint("duracao_ms >= 0", name="ck_verificacao_disponibilidade_duracao"),
        sa.CheckConstraint("status_http IS NULL OR (status_http >= 100 AND status_http <= 599)",
                           name="ck_verificacao_disponibilidade_http"),
        mysql_engine="InnoDB")
    op.create_index("ix_verificacao_disponibilidade_alvo_instante", "verificacoes_disponibilidade", ["alvo", "instante"])


def downgrade():
    op.drop_table("verificacoes_disponibilidade")
