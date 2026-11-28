"""US27-T03: índice de cobertura das consultas de série e da API de indicadores.

Medição com 50 usuários (docs/desempenho/US27-antes.json): MIN/MAX do histórico da
linha (média de 17,5 s), COUNT do período (7 s) e a página com funções de janela
(10 s) liam cada linha da tabela para conferir ``parada_codigo``, ``trecho_id``,
``periodo_fim`` e ``unidade``, que o índice existente
(linha, sentido, indicador, periodo_inicio) não tem. Com estas colunas no índice,
as três consultas passam a ser respondidas só pelo índice. Não altera dados.
No MySQL 8 o CREATE INDEX é online (INPLACE).

Revision ID: us27_0030
Revises: us28_0029
"""
from alembic import op

revision = "us27_0030"
down_revision = "us28_0029"
branch_labels = None
depends_on = None

# parada/trecho antes das datas: o painel filtra a série por igualdade (parada IS NULL ou a
# parada pedida), então o MIN do histórico sai direto do índice e o período é um intervalo dele.
COLUNAS = ["linha_codigo", "sentido", "indicador", "parada_codigo", "trecho_id",
           "periodo_inicio", "periodo_fim", "unidade"]


def upgrade():
    op.create_index("ix_indicadores_serie_cobertura", "indicadores_operacionais", COLUNAS)


def downgrade():
    op.drop_index("ix_indicadores_serie_cobertura", table_name="indicadores_operacionais")
