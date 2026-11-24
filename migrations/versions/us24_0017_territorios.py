"""Version GeoSampa boundaries and assign GTFS stops/segments once.

Revision ID: us24_0017
Revises: us14_0016
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = "us24_0017"
down_revision = "us14_0016"
branch_labels = None
depends_on = None

CODIGO = sa.String(64).with_variant(
    mysql.VARCHAR(64, collation="utf8mb4_0900_bin"), "mysql"
)
INSTANTE = sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")


def upgrade():
    op.create_table(
        "versoes_territoriais",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("fonte", sa.String(255), nullable=False),
        sa.Column("url_fonte", sa.String(1024), nullable=False),
        sa.Column("versao_fonte", sa.String(64), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("crs_origem", sa.String(32), nullable=False),
        sa.Column("capturada_em", INSTANTE, nullable=False),
        sa.Column("metadados", sa.JSON(), nullable=False),
        sa.UniqueConstraint("sha256", name="uq_versao_territorial_sha256"),
        sa.CheckConstraint("crs_origem <> ''", name="ck_versao_territorial_crs"),
        mysql_engine="InnoDB",
    )
    op.create_table(
        "unidades_territoriais",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("versao_id", sa.String(36), nullable=False),
        sa.Column("tipo", sa.String(16), nullable=False),
        sa.Column("codigo", sa.String(32), nullable=False),
        sa.Column("nome", sa.String(128), nullable=False),
        sa.Column("identificador_fonte", sa.String(32), nullable=False),
        sa.Column("subprefeitura_id", sa.String(36), nullable=True),
        sa.Column("geometria", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["versao_id"], ["versoes_territoriais.id"],
            name="fk_unidade_territorial_versao", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["subprefeitura_id", "versao_id"],
            ["unidades_territoriais.id", "unidades_territoriais.versao_id"],
            name="fk_unidade_subprefeitura_versao", ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("versao_id", "tipo", "codigo", name="uq_unidade_territorial_codigo"),
        sa.UniqueConstraint("id", "versao_id", name="uq_unidade_territorial_id_versao"),
        sa.CheckConstraint("tipo IN ('DISTRITO', 'SUBPREFEITURA')", name="ck_unidade_territorial_tipo"),
        sa.CheckConstraint(
            "(tipo = 'DISTRITO' AND subprefeitura_id IS NOT NULL) OR "
            "(tipo = 'SUBPREFEITURA' AND subprefeitura_id IS NULL)",
            name="ck_unidade_territorial_hierarquia",
        ),
        mysql_engine="InnoDB",
    )
    op.create_index(
        "ix_unidade_territorial_versao_tipo", "unidades_territoriais", ["versao_id", "tipo"]
    )

    op.create_table(
        "paradas_territorios",
        sa.Column("versao_gtfs_id", sa.String(36), primary_key=True),
        sa.Column("parada_codigo", CODIGO, primary_key=True),
        sa.Column("versao_territorial_id", sa.String(36), primary_key=True),
        sa.Column("distrito_id", sa.String(36), nullable=False),
        sa.Column("subprefeitura_id", sa.String(36), nullable=False),
        sa.Column("ponto_na_fronteira", sa.Boolean(), nullable=False),
        sa.Column("qualidade", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["versao_gtfs_id", "parada_codigo"], ["paradas.versao_gtfs_id", "paradas.codigo"],
            name="fk_parada_territorio_rede", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["distrito_id", "versao_territorial_id"],
            ["unidades_territoriais.id", "unidades_territoriais.versao_id"],
            name="fk_parada_territorio_distrito", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["subprefeitura_id", "versao_territorial_id"],
            ["unidades_territoriais.id", "unidades_territoriais.versao_id"],
            name="fk_parada_territorio_subprefeitura", ondelete="RESTRICT",
        ),
        mysql_engine="InnoDB",
    )
    op.create_index(
        "ix_parada_territorio_distrito", "paradas_territorios",
        ["versao_territorial_id", "distrito_id"],
    )
    op.create_index(
        "ix_parada_territorio_subprefeitura", "paradas_territorios",
        ["versao_territorial_id", "subprefeitura_id"],
    )

    op.create_table(
        "trechos_territorios",
        sa.Column("trecho_id", sa.String(36), primary_key=True),
        sa.Column("versao_gtfs_id", sa.String(36), primary_key=True),
        sa.Column("linha_codigo", CODIGO, primary_key=True),
        sa.Column("versao_territorial_id", sa.String(36), primary_key=True),
        sa.Column("distrito_id", sa.String(36), nullable=False),
        sa.Column("subprefeitura_id", sa.String(36), nullable=False),
        sa.Column("fracao_distrito", sa.Double(), nullable=False),
        sa.Column("fracao_subprefeitura", sa.Double(), nullable=False),
        sa.Column("cruza_limite", sa.Boolean(), nullable=False),
        sa.Column("qualidade", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["trecho_id", "versao_gtfs_id", "linha_codigo"],
            ["trechos.id", "trechos.versao_gtfs_id", "trechos.linha_codigo"],
            name="fk_trecho_territorio_rede", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["distrito_id", "versao_territorial_id"],
            ["unidades_territoriais.id", "unidades_territoriais.versao_id"],
            name="fk_trecho_territorio_distrito", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["subprefeitura_id", "versao_territorial_id"],
            ["unidades_territoriais.id", "unidades_territoriais.versao_id"],
            name="fk_trecho_territorio_subprefeitura", ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "fracao_distrito > 0 AND fracao_distrito <= 1", name="ck_trecho_territorio_fracao_distrito"
        ),
        sa.CheckConstraint(
            "fracao_subprefeitura > 0 AND fracao_subprefeitura <= 1",
            name="ck_trecho_territorio_fracao_subpref",
        ),
        mysql_engine="InnoDB",
    )
    op.create_index(
        "ix_trecho_territorio_distrito", "trechos_territorios",
        ["versao_territorial_id", "distrito_id"],
    )
    op.create_index(
        "ix_trecho_territorio_subprefeitura", "trechos_territorios",
        ["versao_territorial_id", "subprefeitura_id"],
    )


def downgrade():
    op.drop_table("trechos_territorios")
    op.drop_table("paradas_territorios")
    op.drop_index("ix_unidade_territorial_versao_tipo", table_name="unidades_territoriais")
    op.drop_table("unidades_territoriais")
    op.drop_table("versoes_territoriais")
