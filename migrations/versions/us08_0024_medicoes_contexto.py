"""US08-T02: MedicaoContexto (diagrama, página 4) para a velocidade por trecho.

Revision ID: us08_0024
Revises: merge_0023
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = "us08_0024"
down_revision = "merge_0023"
branch_labels = None
depends_on = None

INSTANTE = sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")


def upgrade():
    op.create_table(
        "medicoes_contexto",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("chave", sa.String(64), nullable=False),
        sa.Column("tipo", sa.String(16), nullable=False),
        sa.Column("trecho_id", sa.String(36), nullable=False),
        sa.Column("inicio", INSTANTE, nullable=False),
        sa.Column("fim", INSTANTE, nullable=True),
        sa.Column("fonte", sa.String(64), nullable=False),
        sa.Column("versao_fonte", sa.String(128), nullable=False),
        sa.Column("dados", sa.JSON(), nullable=False),
        sa.Column("unidades", sa.JSON(), nullable=False),
        sa.Column("coletada_em", INSTANTE, nullable=False),
        sa.ForeignKeyConstraint(["trecho_id"], ["trechos.id"],
                                name="fk_medicoes_contexto_trecho", ondelete="RESTRICT"),
        sa.UniqueConstraint("chave", name="uq_medicoes_contexto_chave"),
        sa.CheckConstraint("tipo IN ('CLIMA', 'INFRAESTRUTURA', 'TRAFEGO')",
                           name="ck_medicoes_contexto_tipo"),
        sa.CheckConstraint("fim IS NULL OR fim > inicio", name="ck_medicoes_contexto_periodo"),
        mysql_engine="InnoDB",
    )
    op.create_index("ix_medicoes_contexto_trecho_tipo_inicio", "medicoes_contexto",
                    ["trecho_id", "tipo", "inicio"])


def downgrade():
    op.drop_index("ix_medicoes_contexto_trecho_tipo_inicio", table_name="medicoes_contexto")
    op.drop_table("medicoes_contexto")
