"""Version bus corridors and exclusive bus lanes (US07).

Revision ID: us07_0020
Revises: us15_0019
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = "us07_0020"
down_revision = "us15_0019"
branch_labels = None
depends_on = None

INSTANTE = sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")


def upgrade():
    op.create_table(
        "versoes_infraestrutura",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("fonte", sa.String(64), nullable=False),
        sa.Column("url_fonte", sa.String(1024), nullable=False),
        sa.Column("versao_fonte", sa.String(128), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("crs_origem", sa.String(32), nullable=False),
        sa.Column("capturada_em", INSTANTE, nullable=False),
        sa.Column("metadados", sa.JSON(), nullable=False),
        sa.UniqueConstraint("fonte", "sha256", name="uq_versao_infra_fonte_hash"),
        sa.CheckConstraint("crs_origem <> ''", name="ck_versao_infra_crs"),
        mysql_engine="InnoDB",
    )
    op.create_table(
        "classificacoes_infraestrutura",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("versao_id", sa.String(36), nullable=False),
        sa.Column("identificador_fonte", sa.String(128), nullable=False),
        sa.Column("categoria", sa.String(32), nullable=False),
        sa.Column("geometria", sa.JSON(), nullable=False),
        sa.Column("atributos", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["versao_id"], ["versoes_infraestrutura.id"],
                                name="fk_classificacao_infra_versao", ondelete="RESTRICT"),
        sa.UniqueConstraint("versao_id", "identificador_fonte", name="uq_classificacao_infra_origem"),
        sa.CheckConstraint("categoria IN ('CORREDOR', 'FAIXA_EXCLUSIVA')",
                           name="ck_classificacao_infra_categoria"),
        mysql_engine="InnoDB",
    )
    op.create_index("ix_classificacao_infra_versao_categoria",
                    "classificacoes_infraestrutura", ["versao_id", "categoria"])


def downgrade():
    op.drop_index("ix_classificacao_infra_versao_categoria", table_name="classificacoes_infraestrutura")
    op.drop_table("classificacoes_infraestrutura")
    op.drop_table("versoes_infraestrutura")
