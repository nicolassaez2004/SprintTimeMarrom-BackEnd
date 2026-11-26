"""Merge US06 (clima) and US07 (infraestrutura) migration branches.

Both branches start at us15_0019 and touch disjoint tables; without this merge
`flask db upgrade` stops with "Multiple head revisions". No DDL.

Revision ID: merge_0023
Revises: us06_0021, us07_0022
"""

revision = "merge_0023"
down_revision = ("us06_0021", "us07_0022")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
