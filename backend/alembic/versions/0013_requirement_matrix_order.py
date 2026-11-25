"""add matrix_row_order and matrix_sheet_order for workbook ordering

Revision ID: 0013
Revises: 0012
"""
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE requirement "
        "ADD COLUMN IF NOT EXISTS matrix_row_order INTEGER"
    )
    op.execute(
        "ALTER TABLE requirement "
        "ADD COLUMN IF NOT EXISTS matrix_sheet_order INTEGER"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE requirement DROP COLUMN IF EXISTS matrix_sheet_order")
    op.execute("ALTER TABLE requirement DROP COLUMN IF EXISTS matrix_row_order")
