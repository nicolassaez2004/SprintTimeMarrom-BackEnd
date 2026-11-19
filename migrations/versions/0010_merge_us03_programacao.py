"""Une as migrações independentes US03 e US04 sem renumerar a main.

Revision ID: 0010_us03_programacao
Revises: 0008, us03_0009
"""
revision = '0010_us03_programacao'
down_revision = ('0008', 'us03_0009')
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    raise RuntimeError('Revisar separadamente as migrações US03 e US04 antes de restaurar backup.')
