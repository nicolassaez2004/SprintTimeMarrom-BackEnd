"""Persist per-segment infrastructure classifications (US07-T02).

Revision ID: us07_0022
Revises: us07_0021
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = "us07_0022"
down_revision = "us07_0021"
branch_labels = None
depends_on = None

INSTANTE = sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")


def upgrade():
    op.create_table(
        "associacoes_infraestrutura_trechos",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("trecho_id", sa.String(36), nullable=False),
        sa.Column("versao_conjunto", sa.String(64), nullable=False),
        sa.Column("versoes_infraestrutura", sa.JSON(), nullable=False),
        sa.Column("classificacao", sa.String(32), nullable=False),
        sa.Column("fracao_corredor", sa.Float(), nullable=False),
        sa.Column("fracao_faixa", sa.Float(), nullable=False),
        sa.Column("fracao_sem_cobertura", sa.Float(), nullable=False),
        sa.Column("categorias_confirmadas", sa.JSON(), nullable=False),
        sa.Column("feicoes_associadas", sa.JSON(), nullable=False),
        sa.Column("regra", sa.JSON(), nullable=False),
        sa.Column("calculada_em", INSTANTE, nullable=False),
        sa.ForeignKeyConstraint(["trecho_id"], ["trechos.id"],
                                name="fk_assoc_infra_trecho", ondelete="RESTRICT"),
        sa.UniqueConstraint("trecho_id", "versao_conjunto", name="uq_assoc_infra_trecho_conjunto"),
        sa.CheckConstraint(
            "classificacao IN ('CORREDOR', 'FAIXA_EXCLUSIVA', 'MISTO', "
            "'COBERTURA_PARCIAL', 'SEM_COBERTURA')", name="ck_assoc_infra_classificacao"),
        sa.CheckConstraint("fracao_corredor >= 0 AND fracao_corredor <= 1",
                           name="ck_assoc_infra_fracao_corredor"),
        sa.CheckConstraint("fracao_faixa >= 0 AND fracao_faixa <= 1",
                           name="ck_assoc_infra_fracao_faixa"),
        sa.CheckConstraint("fracao_sem_cobertura >= 0 AND fracao_sem_cobertura <= 1",
                           name="ck_assoc_infra_fracao_sem_cobertura"),
        mysql_engine="InnoDB",
    )
    op.create_index("ix_assoc_infra_conjunto_classificacao",
                    "associacoes_infraestrutura_trechos", ["versao_conjunto", "classificacao"])


def downgrade():
    op.drop_index("ix_assoc_infra_conjunto_classificacao", table_name="associacoes_infraestrutura_trechos")
    op.drop_table("associacoes_infraestrutura_trechos")
