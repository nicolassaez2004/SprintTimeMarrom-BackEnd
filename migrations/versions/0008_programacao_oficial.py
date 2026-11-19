"""Capturas oficiais e partidas programadas com procedência (US04-T01).

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-19
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

CODIGO = sa.String(64).with_variant(mysql.VARCHAR(64, collation="utf8mb4_0900_bin"), "mysql")
INSTANTE = sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")


def upgrade():
    op.create_table(
        "importacoes_programacao",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("versao_gtfs_id", sa.String(36), nullable=False),
        sa.Column("linha_codigo", CODIGO, nullable=False),
        sa.Column("data_operacao", sa.Date(), nullable=False),
        sa.Column("sentido", sa.String(1), nullable=False),
        sa.Column("tipo_dia", sa.String(12), nullable=False),
        sa.Column("fonte", sa.String(8), nullable=False),
        sa.Column("versao_fonte", sa.String(64), nullable=False),
        sa.Column("fonte_url", sa.String(512), nullable=False),
        sa.Column("capturada_em", INSTANTE, nullable=False),
        sa.Column("verificada_em", INSTANTE, nullable=False),
        sa.Column("arquivo_id", sa.String(36), nullable=False),
        sa.ForeignKeyConstraint(
            ["versao_gtfs_id", "linha_codigo"], ["linhas.versao_gtfs_id", "linhas.codigo"],
            name="fk_importacoes_programacao_linha", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["arquivo_id"], ["arquivos_armazenados.id"],
            name="fk_importacoes_programacao_arquivo", ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("versao_gtfs_id", "linha_codigo", "data_operacao", "sentido",
                            "tipo_dia", "fonte", "versao_fonte", name="uq_importacoes_programacao_contexto"),
        sa.CheckConstraint("sentido IN ('0', '1')", name="ck_importacoes_programacao_sentido"),
        sa.CheckConstraint("tipo_dia IN ('UTIL', 'SABADO', 'DOMINGO', 'FERIADO')",
                           name="ck_importacoes_programacao_tipo_dia"),
        sa.CheckConstraint("fonte IN ('OFICIAL', 'GTFS')", name="ck_importacoes_programacao_fonte"),
    )
    op.create_table(
        "partidas_programadas",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("importacao_id", sa.String(36), nullable=False),
        sa.Column("ordem", sa.Integer(), nullable=False),
        sa.Column("saida_prevista", INSTANTE, nullable=False),
        sa.Column("tipo_oferta", sa.String(12), nullable=False),
        sa.Column("intervalo_seg", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["importacao_id"], ["importacoes_programacao.id"],
            name="fk_partidas_programadas_importacao", ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("importacao_id", "ordem", name="uq_partidas_programadas_ordem"),
        sa.CheckConstraint("ordem >= 1", name="ck_partidas_programadas_ordem"),
        sa.CheckConstraint("tipo_oferta IN ('FIXA', 'FREQUENCIA')", name="ck_partidas_programadas_tipo"),
        sa.CheckConstraint("(tipo_oferta = 'FIXA' AND intervalo_seg IS NULL) OR "
                           "(tipo_oferta = 'FREQUENCIA' AND intervalo_seg IS NOT NULL AND intervalo_seg > 0)",
                           name="ck_partidas_programadas_intervalo"),
    )


def downgrade():
    raise RuntimeError("Downgrade da 0008 apagaria programação e sua procedência; restaure de um backup.")
