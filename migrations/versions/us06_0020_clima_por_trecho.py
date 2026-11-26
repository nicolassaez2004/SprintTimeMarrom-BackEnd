"""US06: estações, observações e coletas de clima; vínculo trecho → estação.

Revision ID: us06_0020
Revises: us15_0019
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = "us06_0020"
down_revision = "us15_0019"
branch_labels = None
depends_on = None

CODIGO = sa.String(64).with_variant(
    mysql.VARCHAR(64, collation="utf8mb4_0900_bin"), "mysql"
)
INSTANTE = sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")
ID_GRANDE = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade():
    op.create_table(
        "estacoes_meteorologicas",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("fonte", sa.String(16), nullable=False),
        sa.Column("codigo", sa.String(32), nullable=False),
        sa.Column("nome", sa.String(255), nullable=False),
        sa.Column("latitude", sa.Numeric(11, 8), nullable=False),
        sa.Column("longitude", sa.Numeric(11, 8), nullable=False),
        sa.Column("altitude_m", sa.Numeric(8, 2), nullable=True),
        sa.Column("intervalo_seg", sa.Integer(), nullable=False),
        sa.Column("operacao_inicio", sa.Date(), nullable=True),
        sa.Column("situacao_fonte", sa.String(32), nullable=True),
        sa.Column("versao_fonte", sa.String(64), nullable=False),
        sa.Column("atualizada_em", INSTANTE, nullable=False),
        sa.UniqueConstraint("fonte", "codigo", name="uq_estacoes_meteorologicas_fonte_codigo"),
        sa.CheckConstraint("fonte IN ('INMET', 'CGE')", name="ck_estacoes_meteorologicas_fonte"),
        sa.CheckConstraint("latitude BETWEEN -90 AND 90", name="ck_estacoes_meteorologicas_latitude"),
        sa.CheckConstraint("longitude BETWEEN -180 AND 180",
                           name="ck_estacoes_meteorologicas_longitude"),
        sa.CheckConstraint("intervalo_seg > 0", name="ck_estacoes_meteorologicas_intervalo"),
        mysql_engine="InnoDB",
    )
    op.create_table(
        "coletas_clima",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("fonte", sa.String(16), nullable=False),
        sa.Column("prevista_em", INSTANTE, nullable=False),
        sa.Column("iniciada_em", INSTANTE, nullable=False),
        sa.Column("finalizada_em", INSTANTE, nullable=True),
        sa.Column("estado", sa.String(16), nullable=False),
        sa.Column("periodo_inicio", INSTANTE, nullable=True),
        sa.Column("periodo_fim", INSTANTE, nullable=True),
        sa.Column("recebidas", sa.Integer(), nullable=False),
        sa.Column("novas", sa.Integer(), nullable=False),
        sa.Column("erro", sa.String(255), nullable=True),
        sa.CheckConstraint("fonte IN ('INMET', 'CGE')", name="ck_coletas_clima_fonte"),
        sa.CheckConstraint(
            "estado IN ('EM_ANDAMENTO', 'OK', 'PARCIAL', 'FALHA', 'INDISPONIVEL')",
            name="ck_coletas_clima_estado",
        ),
        mysql_engine="InnoDB",
    )
    op.create_index("ix_coletas_clima_fonte_iniciada", "coletas_clima", ["fonte", "iniciada_em"])
    op.create_table(
        "observacoes_clima",
        sa.Column("id", ID_GRANDE, primary_key=True, autoincrement=True),
        sa.Column("estacao_id", sa.String(36), nullable=False),
        sa.Column("instante", INSTANTE, nullable=False),
        sa.Column("intervalo_seg", sa.Integer(), nullable=False),
        sa.Column("chuva", sa.Numeric(7, 2), nullable=True),
        sa.Column("unidade_chuva", sa.String(8), nullable=False),
        sa.Column("temperatura", sa.Numeric(5, 2), nullable=True),
        sa.Column("unidade_temperatura", sa.String(8), nullable=False),
        sa.Column("fonte", sa.String(16), nullable=False),
        sa.Column("versao_fonte", sa.String(64), nullable=False),
        sa.Column("coletada_em", INSTANTE, nullable=False),
        sa.Column("coleta_id", sa.String(36), nullable=True),
        sa.ForeignKeyConstraint(["estacao_id"], ["estacoes_meteorologicas.id"],
                                name="fk_observacoes_clima_estacao", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["coleta_id"], ["coletas_clima.id"],
                                name="fk_observacoes_clima_coleta", ondelete="RESTRICT"),
        sa.UniqueConstraint("estacao_id", "instante", name="uq_observacoes_clima_estacao_instante"),
        sa.CheckConstraint("chuva IS NULL OR chuva >= 0", name="ck_observacoes_clima_chuva"),
        sa.CheckConstraint("intervalo_seg > 0", name="ck_observacoes_clima_intervalo"),
        sa.CheckConstraint("unidade_chuva = 'mm'", name="ck_observacoes_clima_unidade_chuva"),
        sa.CheckConstraint("unidade_temperatura = 'degC'",
                           name="ck_observacoes_clima_unidade_temperatura"),
        mysql_engine="InnoDB",
    )
    op.create_index("ix_observacoes_clima_instante", "observacoes_clima", ["instante"])
    op.create_table(
        "trechos_estacoes_clima",
        sa.Column("trecho_id", sa.String(36), primary_key=True),
        sa.Column("fonte", sa.String(16), primary_key=True),
        sa.Column("versao_gtfs_id", sa.String(36), nullable=False),
        sa.Column("linha_codigo", CODIGO, nullable=False),
        sa.Column("estacao_id", sa.String(36), nullable=True),
        sa.Column("distancia_m", sa.Double(), nullable=True),
        sa.Column("raio_maximo_m", sa.Double(), nullable=False),
        sa.Column("situacao", sa.String(16), nullable=False),
        sa.Column("crs_medicao", sa.String(16), nullable=False),
        sa.Column("versao_regra", sa.String(32), nullable=False),
        sa.Column("calculado_em", INSTANTE, nullable=False),
        sa.ForeignKeyConstraint(
            ["trecho_id", "versao_gtfs_id", "linha_codigo"],
            ["trechos.id", "trechos.versao_gtfs_id", "trechos.linha_codigo"],
            name="fk_trecho_estacao_clima_rede", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["estacao_id"], ["estacoes_meteorologicas.id"],
                                name="fk_trecho_estacao_clima_estacao", ondelete="RESTRICT"),
        sa.CheckConstraint("fonte IN ('INMET', 'CGE')", name="ck_trecho_estacao_clima_fonte"),
        sa.CheckConstraint(
            "situacao IN ('ASSOCIADO', 'FORA_DO_RAIO', 'SEM_ESTACAO')",
            name="ck_trecho_estacao_clima_situacao",
        ),
        sa.CheckConstraint(
            "(situacao = 'SEM_ESTACAO' AND estacao_id IS NULL AND distancia_m IS NULL) OR "
            "(situacao <> 'SEM_ESTACAO' AND estacao_id IS NOT NULL AND distancia_m >= 0)",
            name="ck_trecho_estacao_clima_coerencia",
        ),
        mysql_engine="InnoDB",
    )
    op.create_index("ix_trecho_estacao_clima_estacao", "trechos_estacoes_clima", ["estacao_id"])


def downgrade():
    # Os índices saem com as tabelas; no MySQL, apagar antes o índice que
    # sustenta uma chave estrangeira falharia.
    op.drop_table("trechos_estacoes_clima")
    op.drop_table("observacoes_clima")
    op.drop_table("coletas_clima")
    op.drop_table("estacoes_meteorologicas")
