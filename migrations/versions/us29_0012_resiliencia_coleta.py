"""Lacunas, posse, caixa de alertas e confirmação medida de coleta.

Revision ID: us29_0012
Revises: 0011_merge_us12_us03
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = "us29_0012"
down_revision = "0011_merge_us12_us03"
branch_labels = None
depends_on = None


def upgrade():
    instante = mysql.DATETIME(fsp=6)
    op.create_table(
        "lacunas_coleta",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("inicio", instante, nullable=False),
        sa.Column("fim", instante),
        sa.Column("causa", sa.String(128), nullable=False),
        sa.Column("fonte", sa.String(32), nullable=False),
        sa.Column("aberta_fonte", sa.String(32), unique=True),
        sa.CheckConstraint("fim IS NULL OR fim >= inicio", name="ck_lacuna_intervalo"),
    )
    op.create_table(
        "estados_coleta",
        sa.Column("fonte", sa.String(32), primary_key=True),
        sa.Column("token", sa.String(36), nullable=False),
    )
    op.create_table(
        "notificacoes_coleta",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("lacuna_id", sa.String(36), nullable=True),
        sa.Column("tipo", sa.String(16), nullable=False),
        sa.Column("criada_em", instante, nullable=False),
        sa.Column("enviada_em", instante),
        sa.Column("canal", sa.String(32), nullable=False),
        sa.ForeignKeyConstraint(
            ["lacuna_id"],
            ["lacunas_coleta.id"],
            name="fk_notificacao_lacuna",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("lacuna_id", "tipo", name="uq_notificacao_lacuna_tipo"),
    )
    for coluna in [
        sa.Column("chave_logica", sa.String(36)),
        sa.Column("lacuna_id", sa.String(36)),
        sa.Column("confirmada_em", instante),
        sa.Column("duracao_gravacao_ms", sa.BigInteger()),
    ]:
        op.add_column("execucoes_coleta", coluna)
    op.create_unique_constraint(
        "uq_execucoes_coleta_chave_logica", "execucoes_coleta", ["chave_logica"]
    )
    op.create_foreign_key(
        "fk_execucao_coleta_lacuna",
        "execucoes_coleta",
        "lacunas_coleta",
        ["lacuna_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade():
    raise RuntimeError(
        "Reverter perderia histórico de falhas e confirmação; restaurar backup."
    )
