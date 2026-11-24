"""Trips observed and versioned indicators with dimensions/counts (US14).

Revision ID: us14_0016
Revises: us11_0016
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = "us14_0016"
down_revision = "us11_0016"
branch_labels = None
depends_on = None

CODIGO = sa.String(64).with_variant(
    mysql.VARCHAR(64, collation="utf8mb4_0900_bin"), "mysql"
)
INSTANTE = sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")


def upgrade():
    with op.batch_alter_table("trechos") as batch:
        batch.create_unique_constraint(
            "uq_trechos_id_versao_linha", ["id", "versao_gtfs_id", "linha_codigo"]
        )

    op.create_table(
        "viagens_observadas",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("partida_programada_id", sa.String(36), nullable=False),
        sa.Column("saida_observada", INSTANTE, nullable=False),
        sa.Column("fim_observado", INSTANTE, nullable=True),
        sa.Column("cobertura_tracado", sa.Double(), nullable=False),
        sa.Column("versao_reconstrucao", sa.String(128), nullable=False),
        sa.Column("versao_correspondencia", sa.String(128), nullable=False),
        sa.Column("qualidade", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["partida_programada_id"], ["partidas_programadas.id"],
            name="fk_viagem_observada_partida", ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "partida_programada_id", "versao_reconstrucao",
            name="uq_viagem_observada_partida_versao",
        ),
        sa.CheckConstraint("cobertura_tracado BETWEEN 0 AND 1",
                           name="ck_viagem_observada_cobertura"),
        sa.CheckConstraint("fim_observado IS NULL OR fim_observado >= saida_observada",
                           name="ck_viagem_observada_intervalo"),
        mysql_engine="InnoDB",
    )
    op.create_index("ix_viagens_observadas_partida_programada_id",
                    "viagens_observadas", ["partida_programada_id"])
    op.create_index("ix_viagens_observadas_reconstrucao",
                    "viagens_observadas", ["versao_reconstrucao"])

    with op.batch_alter_table("indicadores_operacionais") as batch:
        batch.alter_column("linha_codigo", existing_type=sa.String(64), type_=CODIGO,
                            existing_nullable=False)
        batch.alter_column("valor", existing_type=sa.Double(), nullable=True,
                            existing_nullable=False)
        batch.add_column(sa.Column("versao_gtfs_id", sa.String(36), nullable=False))
        batch.add_column(sa.Column("sentido", sa.String(1), nullable=True))
        batch.add_column(sa.Column("parada_codigo", CODIGO, nullable=True))
        batch.add_column(sa.Column("trecho_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("contagem_numerador", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("contagem_denominador", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("versao_correspondencia", sa.String(128), nullable=True))
        batch.add_column(sa.Column("versao_calculo", sa.String(128), nullable=True))
        batch.add_column(sa.Column("chave_resultado", sa.String(64), nullable=True))
        batch.drop_constraint("uq_indicador_publicado_periodo_versao", type_="unique")
        batch.create_foreign_key(
            "fk_indicadores_linha_gtfs", "linhas",
            ["versao_gtfs_id", "linha_codigo"], ["versao_gtfs_id", "codigo"],
            ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            "fk_indicadores_parada_gtfs", "paradas",
            ["versao_gtfs_id", "parada_codigo"], ["versao_gtfs_id", "codigo"],
            ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            "fk_indicadores_trecho_linha", "trechos",
            ["trecho_id", "versao_gtfs_id", "linha_codigo"],
            ["id", "versao_gtfs_id", "linha_codigo"], ondelete="RESTRICT",
        )
        batch.create_check_constraint(
            "ck_indicador_parada_ou_trecho", "parada_codigo IS NULL OR trecho_id IS NULL"
        )
        batch.create_check_constraint("ck_indicador_sentido", "sentido IS NULL OR sentido IN ('0', '1')")
        batch.create_check_constraint("ck_indicador_numerador", "contagem_numerador IS NULL OR contagem_numerador >= 0")
        batch.create_check_constraint("ck_indicador_denominador", "contagem_denominador IS NULL OR contagem_denominador >= 0")
        batch.create_check_constraint(
            "ck_indicador_contagens",
            "contagem_numerador IS NULL OR contagem_denominador IS NULL OR contagem_numerador <= contagem_denominador",
        )
        batch.create_check_constraint(
            "ck_indicador_denominador_zero_sem_valor",
            "contagem_denominador IS NULL OR contagem_denominador > 0 OR valor IS NULL",
        )
        batch.drop_index("ix_indicadores_linha_indicador_inicio")
        batch.create_index("ix_indicadores_linha_sentido_tipo_periodo",
                           ["linha_codigo", "sentido", "indicador", "periodo_inicio"])
        batch.create_index("ix_indicadores_parada_periodo", ["parada_codigo", "periodo_inicio"])
        batch.create_index("ix_indicadores_trecho_periodo", ["trecho_id", "periodo_inicio"])

    op.execute(sa.text(
        "UPDATE indicadores_operacionais SET chave_resultado = id "
        "WHERE chave_resultado IS NULL"
    ))
    with op.batch_alter_table("indicadores_operacionais") as batch:
        batch.alter_column("chave_resultado", existing_type=sa.String(64), nullable=False,
                            existing_nullable=True)
        batch.create_unique_constraint("uq_indicadores_chave_resultado", ["chave_resultado"])


def downgrade():
    raise RuntimeError(
        "Reverter US14 apagaria viagens observadas e permitiria descartar valores nulos; restaure de backup."
    )
