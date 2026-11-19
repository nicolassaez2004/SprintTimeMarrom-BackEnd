"""Revisões explícitas de correspondência preservam a tabela anterior.

Revision ID: us03_0009
Revises: us03_0008
"""
from alembic import op
import sqlalchemy as sa

revision = 'us03_0009'
down_revision = 'us03_0008'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('tabelas_correspondencia', sa.Column('substitui_id', sa.String(36), nullable=True))
    op.create_foreign_key('fk_tabela_correspondencia_anterior', 'tabelas_correspondencia',
        'tabelas_correspondencia', ['substitui_id', 'versao_gtfs_id'],
        ['id', 'versao_gtfs_id'], ondelete='RESTRICT')
    op.create_check_constraint('ck_tabela_correspondencia_nao_auto', 'tabelas_correspondencia',
                              'substitui_id IS NULL OR substitui_id <> id')


def downgrade():
    raise RuntimeError('Downgrade perderia a proveniência das revisões; restaurar backup.')
