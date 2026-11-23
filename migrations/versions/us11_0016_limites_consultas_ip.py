"""Contador compartilhado para a API pública US11-T02.

Revision ID: us11_0016
Revises: us11_0015
"""
from alembic import op
import sqlalchemy as sa

revision = "us11_0016"
down_revision = "us11_0015"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("limites_consultas_ip",
        sa.Column("chave_ip", sa.String(64), primary_key=True),
        sa.Column("janela", sa.BigInteger(), nullable=False),
        sa.Column("tentativas", sa.Integer(), nullable=False),
        sa.CheckConstraint("janela >= 0", name="ck_limites_ip_janela"),
        sa.CheckConstraint("tentativas BETWEEN 1 AND 61", name="ck_limites_ip_tentativas"),
        mysql_engine="InnoDB",
    )
    op.create_index("ix_limites_ip_janela", "limites_consultas_ip", ["janela"])


def downgrade():
    raise RuntimeError("Downgrade removeria o orçamento de requisições dos clientes; revisar com o responsável.")
