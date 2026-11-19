"""Índice para a exportação diária dos brutos (US09-T01).

Revision ID: us09_0013
Revises: us29_0012
"""

from alembic import op

revision = "us09_0013"
down_revision = "us29_0012"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("ix_registros_gps_coletado_em_id", "registros_gps", ["coletado_em", "id"])


def downgrade():
    op.drop_index("ix_registros_gps_coletado_em_id", table_name="registros_gps")
