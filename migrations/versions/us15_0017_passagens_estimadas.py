"""Passagens estimadas por parada e viagem (US15-T01).

Revision ID: us15_0017
Revises: us14_0016
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = "us15_0017"
down_revision = "us14_0016"
branch_labels = None
depends_on = None

CODIGO = sa.String(64).with_variant(
    mysql.VARCHAR(64, collation="utf8mb4_0900_bin"), "mysql"
)
INSTANTE = sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")
ID_GRANDE = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade():
    op.create_table(
        "passagens_estimadas",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("viagem_observada_id", sa.String(36), nullable=False),
        sa.Column("versao_gtfs_id", sa.String(36), nullable=False),
        sa.Column("parada_codigo", CODIGO, nullable=False),
        sa.Column("instante", INSTANTE, nullable=False),
        sa.Column("metodo", sa.String(64), nullable=False),
        sa.Column("versao_estimativa", sa.String(128), nullable=False),
        sa.Column("posicao_anterior_id", ID_GRANDE, nullable=False),
        sa.Column("posicao_posterior_id", ID_GRANDE, nullable=False),
        sa.ForeignKeyConstraint(["viagem_observada_id"], ["viagens_observadas.id"],
                                name="fk_passagens_estimadas_viagem", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["versao_gtfs_id", "parada_codigo"],
                                ["paradas.versao_gtfs_id", "paradas.codigo"],
                                name="fk_passagens_estimadas_parada", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["posicao_anterior_id"], ["registros_gps.id"],
                                name="fk_passagens_estimadas_anterior", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["posicao_posterior_id"], ["registros_gps.id"],
                                name="fk_passagens_estimadas_posterior", ondelete="RESTRICT"),
        sa.UniqueConstraint("viagem_observada_id", "parada_codigo", "versao_estimativa",
                            name="uq_passagens_estimadas_viagem_parada_versao"),
        mysql_engine="InnoDB",
    )
    op.create_index("ix_passagens_estimadas_parada_instante", "passagens_estimadas",
                    ["versao_gtfs_id", "parada_codigo", "instante"])


def downgrade():
    op.drop_index("ix_passagens_estimadas_parada_instante", table_name="passagens_estimadas")
    op.drop_table("passagens_estimadas")
