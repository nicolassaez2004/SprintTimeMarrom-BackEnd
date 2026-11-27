"""US20-T02C: histórico das tentativas de cada ExecucaoProcessamento.

Uma entrada JSON por tentativa (início, fim, estado, erro). ``erro`` continua
com a última falha; o histórico preserva as anteriores para auditoria.

Revision ID: us20_0025
Revises: us08_0024
"""

from alembic import op
import sqlalchemy as sa

revision = "us20_0025"
down_revision = "us08_0024"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("execucoes_processamento", sa.Column("historico", sa.JSON(), nullable=True))


def downgrade():
    op.drop_column("execucoes_processamento", "historico")
