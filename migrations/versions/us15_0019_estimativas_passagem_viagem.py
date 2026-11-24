"""Execução da estimativa de passagens por viagem (revisão US15-T01/T02).

Revision ID: us15_0019
Revises: us24_0018
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = "us15_0019"
down_revision = "us24_0018"
branch_labels = None
depends_on = None

INSTANTE = sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")


def upgrade():
    op.create_table(
        "estimativas_passagem_viagem",
        sa.Column("viagem_observada_id", sa.String(36), primary_key=True),
        sa.Column("versao_estimativa", sa.String(128), primary_key=True),
        sa.Column("lacuna_maxima_seg", sa.Double(), nullable=False),
        sa.Column("executada_em", INSTANTE, nullable=False),
        sa.Column("sem_evidencia", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["viagem_observada_id"], ["viagens_observadas.id"],
                                name="fk_estimativas_passagem_viagem", ondelete="RESTRICT"),
        mysql_engine="InnoDB",
    )


def downgrade():
    op.drop_table("estimativas_passagem_viagem")
