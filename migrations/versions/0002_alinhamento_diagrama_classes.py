"""Alinha a coleta ao diagrama de classes (revisão de 18/11/2026).

Não destrutiva: só cria, renomeia, amplia tipos e copia valores. Nenhuma linha
e nenhuma coluna com dados é apagada.

- Cria `veiculos` e a preenche com os prefixos já conhecidos.
- `onibus_selecionados.prefixo` passa a referenciar `veiculos`.
- `registros_gps`: o histórico deixa de depender da seleção e passa a apontar
  para `veiculos`; colunas renomeadas para os nomes do diagrama
  (prefixo_origem, linha_origem, coletado_em); coordenadas FLOAT -> DOUBLE;
  novas colunas sentido_origem, horario_gps e payload (JSON).
- `execucoes_coleta`: status -> estado (valores do diagrama),
  quantidade_registros -> novos_persistidos; novas colunas fonte, prevista_em,
  tentativas e recebidos. Para ciclos antigos, prevista_em e recebidos ficam
  NULL (desconhecidos); fonte = OLHO_VIVO e tentativas = 1, como era o único
  comportamento possível antes.

Escrita para MySQL. DDL no MySQL não é transacional: faça backup
(`mysqldump`) antes de aplicar.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-18
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

DATETIME6 = mysql.DATETIME(fsp=6)
ESTADOS = {
    "executando": "EM_ANDAMENTO",
    "concluida": "OK",
    "parcial": "PARCIAL",
    "falha": "FALHA",
}


def _verificar_pre_condicoes(bind):
    if bind.dialect.name != "mysql":
        raise RuntimeError("A migração 0002 foi escrita para MySQL.")
    # Valida os dados antes de qualquer DDL, pois o MySQL não desfaz DDL.
    desconhecidos = [
        valor
        for (valor,) in bind.execute(sa.text("SELECT DISTINCT status FROM execucoes_coleta"))
        if valor not in ESTADOS
    ]
    if desconhecidos:
        raise RuntimeError(
            f"Valores de status sem correspondência no diagrama: {desconhecidos}. "
            "Defina o mapeamento antes de migrar."
        )


def upgrade():
    bind = op.get_bind()
    _verificar_pre_condicoes(bind)

    # --- veiculos --------------------------------------------------------
    op.create_table(
        "veiculos",
        sa.Column("prefixo", sa.String(32), nullable=False),
        sa.PrimaryKeyConstraint("prefixo"),
    )
    op.execute(
        "INSERT INTO veiculos (prefixo) "
        "SELECT prefixo FROM onibus_selecionados "
        "UNION SELECT prefixo_onibus FROM registros_gps"
    )
    op.create_foreign_key(
        "fk_onibus_selecionados_veiculo",
        "onibus_selecionados",
        "veiculos",
        ["prefixo"],
        ["prefixo"],
        ondelete="RESTRICT",
    )

    # --- registros_gps ---------------------------------------------------
    # A FK antiga foi criada sem nome; o MySQL gerou um, que é lido aqui.
    for fk in sa.inspect(bind).get_foreign_keys("registros_gps"):
        if fk["referred_table"] == "onibus_selecionados":
            op.drop_constraint(fk["name"], "registros_gps", type_="foreignkey")

    op.alter_column(
        "registros_gps", "prefixo_onibus",
        new_column_name="prefixo_origem",
        existing_type=sa.String(32), nullable=True,
    )
    op.add_column("registros_gps", sa.Column("veiculo_prefixo", sa.String(32), nullable=True))
    op.execute("UPDATE registros_gps SET veiculo_prefixo = prefixo_origem")
    op.create_foreign_key(
        "fk_registros_gps_veiculo",
        "registros_gps",
        "veiculos",
        ["veiculo_prefixo"],
        ["prefixo"],
        ondelete="RESTRICT",
    )
    op.alter_column(
        "registros_gps", "codigo_linha",
        new_column_name="linha_origem",
        existing_type=sa.Integer(), type_=sa.String(16), existing_nullable=True,
    )
    op.alter_column(
        "registros_gps", "registrado_em",
        new_column_name="coletado_em",
        existing_type=sa.DateTime(), type_=DATETIME6, existing_nullable=False,
    )
    for coluna in ("latitude", "longitude"):
        op.alter_column(
            "registros_gps", coluna,
            existing_type=sa.Float(), type_=sa.Double(), nullable=True,
        )
    op.add_column("registros_gps", sa.Column("sentido_origem", sa.String(8), nullable=True))
    op.add_column("registros_gps", sa.Column("horario_gps", DATETIME6, nullable=True))
    op.add_column("registros_gps", sa.Column("payload", sa.JSON(), nullable=True))
    # Registros antigos não têm horario_gps (NULL), então não há conflito.
    op.create_unique_constraint(
        "uq_registros_gps_prefixo_horario",
        "registros_gps",
        ["prefixo_origem", "horario_gps"],
    )

    # --- execucoes_coleta ------------------------------------------------
    op.alter_column(
        "execucoes_coleta", "status",
        new_column_name="estado",
        existing_type=sa.String(16), existing_nullable=False,
    )
    for antigo, novo in ESTADOS.items():
        op.execute(
            sa.text("UPDATE execucoes_coleta SET estado = :novo WHERE estado = :antigo")
            .bindparams(novo=novo, antigo=antigo)
        )
    op.alter_column(
        "execucoes_coleta", "quantidade_registros",
        new_column_name="novos_persistidos",
        existing_type=sa.Integer(), existing_nullable=False,
    )
    for coluna, nula in (("iniciada_em", False), ("finalizada_em", True)):
        op.alter_column(
            "execucoes_coleta", coluna,
            existing_type=sa.DateTime(), type_=DATETIME6, existing_nullable=nula,
        )
    op.add_column(
        "execucoes_coleta",
        sa.Column("fonte", sa.String(32), nullable=False, server_default="OLHO_VIVO"),
    )
    op.add_column("execucoes_coleta", sa.Column("prevista_em", DATETIME6, nullable=True))
    op.add_column(
        "execucoes_coleta",
        sa.Column("tentativas", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column("execucoes_coleta", sa.Column("recebidos", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "ck_execucoes_coleta_estado",
        "execucoes_coleta",
        "estado IN ('EM_ANDAMENTO', 'OK', 'PARCIAL', 'FALHA')",
    )
    op.create_index("ix_execucoes_coleta_iniciada_em", "execucoes_coleta", ["iniciada_em"])


def downgrade():
    raise RuntimeError(
        "Downgrade da 0002 descartaria payload e colunas novas; restaure de um backup."
    )
