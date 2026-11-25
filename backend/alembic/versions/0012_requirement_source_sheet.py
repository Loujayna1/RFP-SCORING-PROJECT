"""add source_sheet to requirement (matrix extraction)

Revision ID: 0012
Revises: 0011
"""
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE requirement "
        "ADD COLUMN IF NOT EXISTS source_sheet VARCHAR(200)"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE requirement DROP COLUMN IF EXISTS source_sheet")
