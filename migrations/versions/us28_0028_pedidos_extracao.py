"""US28-T01: pedidos de extração de brutos ligados a um job durável.

Revision ID: us28_0028
Revises: us21_0027
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = "us28_0028"
down_revision = "us21_0027"
branch_labels = None
depends_on = None

INSTANTE = sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")


def upgrade():
    op.create_table(
        "pedidos_extracao",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("linha_codigo", sa.String(16), nullable=False),
        sa.Column("inicio_periodo", INSTANTE, nullable=False),
        sa.Column("fim_periodo", INSTANTE, nullable=False),
        sa.Column("formato", sa.String(8), nullable=False),
        sa.Column("criado_em", INSTANTE, nullable=False),
        sa.Column("versao_base", sa.String(128), nullable=False),
        sa.Column("execucao_processamento_id", sa.String(36), nullable=False),
        sa.Column("arquivo_id", sa.String(36), nullable=True),
        sa.CheckConstraint("formato IN ('CSV', 'PARQUET')", name="ck_pedidos_extracao_formato"),
        sa.CheckConstraint("fim_periodo > inicio_periodo", name="ck_pedidos_extracao_periodo"),
        sa.UniqueConstraint("execucao_processamento_id", name="uq_pedidos_extracao_execucao"),
        sa.ForeignKeyConstraint(["execucao_processamento_id"], ["execucoes_processamento.id"],
                                name="fk_pedidos_extracao_execucao", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["arquivo_id"], ["arquivos_armazenados.id"],
                                name="fk_pedidos_extracao_arquivo", ondelete="RESTRICT"),
        mysql_engine="InnoDB",
    )


def downgrade():
    raise RuntimeError("Downgrade recusado: apagaria pedidos de extração e seu rastro.")
