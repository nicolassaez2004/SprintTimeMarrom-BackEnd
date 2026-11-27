"""US21: snapshots de relatórios mensais de associações.

Revision ID: us21_0027
Revises: us30_0026
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = 'us21_0027'
down_revision = 'us30_0026'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'relatorios_associacao_mensal',
        sa.Column('mes', sa.String(7), primary_key=True),
        sa.Column('situacao', sa.String(16), nullable=False),
        sa.Column('dados', sa.JSON(), nullable=False),
        sa.Column('hash_dados', sa.String(64), nullable=False),
        sa.Column('registrado_em', sa.DateTime().with_variant(mysql.DATETIME(fsp=6), 'mysql'), nullable=False),
        sa.CheckConstraint("situacao IN ('PUBLICADO', 'INVALIDO')", name='ck_relatorio_associacao_situacao'),
        mysql_engine='InnoDB',
    )


def downgrade():
    raise RuntimeError('Downgrade recusado: apagaria relatórios de associações publicados.')
