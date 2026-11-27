"""US28-T02: índice para extrair um mês de uma linha sem varrer as demais.

Sem ele, a paginação por (coletado_em, id) percorre os brutos de todas as
linhas do período (~1 bilhão de linhas num mês de coleta completa) para achar
os de uma linha. No MySQL 8 o CREATE INDEX é online (INPLACE, sem bloquear a
coleta). Os registros brutos não são alterados.

Revision ID: us28_0029
Revises: us28_0028
"""
from alembic import op

revision = "us28_0029"
down_revision = "us28_0028"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("ix_registros_gps_linha_coletado_em_id", "registros_gps",
                    ["linha_origem", "coletado_em", "id"])


def downgrade():
    op.drop_index("ix_registros_gps_linha_coletado_em_id", table_name="registros_gps")
