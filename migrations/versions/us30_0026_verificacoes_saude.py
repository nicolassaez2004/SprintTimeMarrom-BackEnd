"""US30: sondagens externas de disponibilidade (VerificacaoSaude).

Origem: ``us30_0025`` da branch US30 (não publicada), renumerada para seguir
``us20_0025`` e evitar duas cabeças. Só cria tabela; não altera dados.

Revision ID: us30_0026
Revises: us20_0025
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = "us30_0026"
down_revision = "us20_0025"
branch_labels = None
depends_on = None


def upgrade():
    instante = sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")
    op.create_table(
        "verificacoes_saude",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("componente", sa.String(16), nullable=False),
        sa.Column("verificada_em", instante, nullable=False),
        sa.Column("estado", sa.String(16), nullable=False),
        sa.Column("latencia_ms", sa.Integer(), nullable=False),
        sa.Column("status_http", sa.Integer(), nullable=True),
        sa.Column("detalhe", sa.String(32), nullable=False),
        sa.Column("alvo", sa.String(64), nullable=False),
        sa.CheckConstraint("componente IN ('API', 'PAINEL')", name="ck_verificacao_saude_componente"),
        sa.CheckConstraint("estado IN ('OK', 'DEGRADADO', 'INDISPONIVEL')",
                           name="ck_verificacao_saude_estado"),
        sa.CheckConstraint("latencia_ms >= 0", name="ck_verificacao_saude_latencia"),
        sa.CheckConstraint("status_http IS NULL OR (status_http >= 100 AND status_http <= 599)",
                           name="ck_verificacao_saude_http"),
        mysql_engine="InnoDB",
    )
    op.create_index("ix_verificacao_saude_componente_instante", "verificacoes_saude",
                    ["componente", "verificada_em"])


def downgrade():
    raise RuntimeError("Downgrade recusado: apagaria o histórico de disponibilidade (US30).")
