"""US06-T03 (revisão): situação GEOMETRIA_INVALIDA no vínculo trecho → estação.

Trecho sem dois pontos distintos válidos passa a ter impedimento explícito, em
vez de interromper o recálculo da versão inteira. Também indexa (fonte, versão
GTFS), usada para substituir os vínculos de uma versão. Compatível: nenhuma
linha existente muda de significado.

Revision ID: us06_0021
Revises: us06_0020
"""

from alembic import op
import sqlalchemy as sa

revision = "us06_0021"
down_revision = "us06_0020"
branch_labels = None
depends_on = None

TABELA = "trechos_estacoes_clima"
SITUACAO_V1 = "situacao IN ('ASSOCIADO', 'FORA_DO_RAIO', 'SEM_ESTACAO')"
COERENCIA_V1 = ("(situacao = 'SEM_ESTACAO' AND estacao_id IS NULL AND distancia_m IS NULL) OR "
                "(situacao <> 'SEM_ESTACAO' AND estacao_id IS NOT NULL AND distancia_m >= 0)")
SITUACAO_V2 = "situacao IN ('ASSOCIADO', 'FORA_DO_RAIO', 'SEM_ESTACAO', 'GEOMETRIA_INVALIDA')"
COERENCIA_V2 = ("(situacao IN ('SEM_ESTACAO', 'GEOMETRIA_INVALIDA') AND estacao_id IS NULL "
                "AND distancia_m IS NULL) OR "
                "(situacao IN ('ASSOCIADO', 'FORA_DO_RAIO') AND estacao_id IS NOT NULL AND distancia_m >= 0)")


def _trocar(situacao, coerencia, de, para):
    with op.batch_alter_table(TABELA) as tabela:
        tabela.drop_constraint("ck_trecho_estacao_clima_situacao", type_="check")
        tabela.drop_constraint("ck_trecho_estacao_clima_coerencia", type_="check")
        tabela.alter_column("situacao", existing_type=sa.String(de), type_=sa.String(para),
                            existing_nullable=False)
        tabela.create_check_constraint("ck_trecho_estacao_clima_situacao", situacao)
        tabela.create_check_constraint("ck_trecho_estacao_clima_coerencia", coerencia)


def upgrade():
    _trocar(SITUACAO_V2, COERENCIA_V2, 16, 24)
    op.create_index("ix_trecho_estacao_clima_fonte_versao", TABELA, ["fonte", "versao_gtfs_id"])


def downgrade():
    # Falha (CHECK) se ainda houver vínculos GEOMETRIA_INVALIDA: recalcule antes com a regra v1.
    op.drop_index("ix_trecho_estacao_clima_fonte_versao", table_name=TABELA)
    _trocar(SITUACAO_V1, COERENCIA_V1, 24, 16)
