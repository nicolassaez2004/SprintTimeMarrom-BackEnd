"""Correspondência versionada e proveniência do tratamento (US03-T01/T04).

Revision ID: us03_0008
Revises: 0007
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = 'us03_0008'
down_revision = '0007'
branch_labels = None
depends_on = None
CODIGO = mysql.VARCHAR(64, collation='utf8mb4_0900_bin')


def upgrade():
    op.create_table('tabelas_correspondencia',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('versao', sa.String(96), nullable=False, unique=True),
        sa.Column('versao_gtfs_id', sa.String(36), nullable=False),
        sa.Column('vigencia_inicio', sa.Date(), nullable=False),
        sa.Column('vigencia_fim', sa.Date(), nullable=True),
        sa.Column('fonte', sa.String(255), nullable=False),
        sa.Column('hash', sa.String(64), nullable=False),
        sa.Column('criada_em', mysql.DATETIME(fsp=6), nullable=False),
        sa.ForeignKeyConstraint(['versao_gtfs_id'], ['versoes_gtfs.id'],
            name='fk_tabela_correspondencia_gtfs', ondelete='RESTRICT'),
        sa.UniqueConstraint('id', 'versao_gtfs_id', name='uq_tabela_correspondencia_gtfs'),
        sa.CheckConstraint('vigencia_fim IS NULL OR vigencia_fim >= vigencia_inicio',
                           name='ck_tabela_correspondencia_vigencia'))
    op.create_table('correspondencias_codigos',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('tabela_id', sa.String(36), nullable=False),
        sa.Column('versao_gtfs_id', sa.String(36), nullable=False),
        sa.Column('codigo_olho_vivo', sa.String(32), nullable=False),
        sa.Column('sentido_olho_vivo', sa.String(8), nullable=False),
        sa.Column('linha_codigo', CODIGO, nullable=True),
        sa.Column('sentido_gtfs', sa.String(8), nullable=True),
        sa.Column('motivo', sa.String(255), nullable=True),
        sa.UniqueConstraint('id','versao_gtfs_id',name='uq_correspondencia_id_gtfs'),
        sa.ForeignKeyConstraint(['tabela_id', 'versao_gtfs_id'],
            ['tabelas_correspondencia.id', 'tabelas_correspondencia.versao_gtfs_id'],
            name='fk_correspondencia_tabela_gtfs', ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['versao_gtfs_id', 'linha_codigo'],
            ['linhas.versao_gtfs_id', 'linhas.codigo'], name='fk_correspondencia_linha', ondelete='RESTRICT'),
        sa.CheckConstraint("sentido_olho_vivo IN ('1','2')", name='ck_correspondencia_sentido_ov'),
        sa.CheckConstraint("sentido_gtfs IS NULL OR sentido_gtfs IN ('0','1')", name='ck_correspondencia_sentido_gtfs'),
        sa.CheckConstraint('(linha_codigo IS NULL AND sentido_gtfs IS NULL) OR '
            '(linha_codigo IS NOT NULL AND sentido_gtfs IS NOT NULL)', name='ck_correspondencia_destino'))
    op.create_index('ix_correspondencia_origem','correspondencias_codigos', ['codigo_olho_vivo','sentido_olho_vivo'])
    op.alter_column('observacoes_tratadas', 'valida', existing_type=sa.Boolean(), nullable=True)
    for coluna in [
        sa.Column('linha_codigo', CODIGO, nullable=True),
        sa.Column('sentido_gtfs', sa.String(8), nullable=True),
        sa.Column('correspondencia_id', sa.String(36), nullable=True),
        sa.Column('motivo_correspondencia', sa.String(64), nullable=True),
        sa.Column('candidatos_correspondencia', sa.JSON(), nullable=True),
        sa.Column('distancia_tracado_m', sa.Float(), nullable=True),
        sa.Column('versao_validacao', sa.String(128), nullable=True),
        sa.Column('processada_em', mysql.DATETIME(fsp=6), nullable=True),
    ]: op.add_column('observacoes_tratadas', coluna)
    op.create_foreign_key('fk_observacoes_linha_versionada','observacoes_tratadas','linhas',
        ['versao_gtfs_id','linha_codigo'],['versao_gtfs_id','codigo'], ondelete='RESTRICT')
    op.create_foreign_key('fk_observacoes_correspondencia','observacoes_tratadas','correspondencias_codigos',
        ['correspondencia_id','versao_gtfs_id'],['id','versao_gtfs_id'],ondelete='RESTRICT')


def downgrade():
    raise RuntimeError('Downgrade perderia proveniência do tratamento; restaurar backup.')
