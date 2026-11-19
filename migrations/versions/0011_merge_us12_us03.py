"""Une US12-T03 à US03/US04 sem alterar os esquemas já aplicados.

Revision ID: 0011_merge_us12_us03
Revises: 0009, 0010_us03_programacao
"""

revision = "0011_merge_us12_us03"
down_revision = ("0009", "0010_us03_programacao")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    raise RuntimeError(
        "Revisão de junção não pode ser revertida isoladamente; restaure de backup."
    )
