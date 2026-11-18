"""Esquema base: tabelas como definidas na main (merge 813b9dc).

Bancos que já têm essas tabelas (criadas antes das migrações) NÃO devem rodar
esta revisão: marque-a como aplicada com `flask --app app db stamp 0001` e
depois rode `flask --app app db upgrade`.

Revision ID: 0001
Revises:
Create Date: 2026-11-18
"""
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "execucoes_coleta",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("iniciada_em", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finalizada_em", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("quantidade_registros", sa.Integer(), nullable=False),
        sa.Column("erro", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "onibus_selecionados",
        sa.Column("prefixo", sa.String(32), nullable=False),
        sa.Column("codigo_linha", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("prefixo"),
    )
    op.create_index(
        "ix_onibus_selecionados_codigo_linha", "onibus_selecionados", ["codigo_linha"]
    )
    op.create_table(
        "registros_gps",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("execucao_id", sa.BigInteger(), nullable=False),
        sa.Column("prefixo_onibus", sa.String(32), nullable=False),
        sa.Column("codigo_linha", sa.Integer(), nullable=True),
        sa.Column("latitude", sa.Float(), nullable=False),
        sa.Column("longitude", sa.Float(), nullable=False),
        sa.Column("registrado_em", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("latitude BETWEEN -90 AND 90", name="ck_registros_gps_latitude"),
        sa.CheckConstraint(
            "longitude BETWEEN -180 AND 180", name="ck_registros_gps_longitude"
        ),
        sa.ForeignKeyConstraint(
            ["execucao_id"], ["execucoes_coleta.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["prefixo_onibus"], ["onibus_selecionados.prefixo"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_registros_gps_onibus_instante",
        "registros_gps",
        ["prefixo_onibus", "registrado_em"],
    )


def downgrade():
    raise RuntimeError("Downgrade da base apagaria dados; restaure de um backup.")
