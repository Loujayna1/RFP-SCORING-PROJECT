"""add req_type and original_text to requirement

Revision ID: 0011
Revises: 0010
"""
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE requirement "
        "ADD COLUMN IF NOT EXISTS req_type VARCHAR(20) DEFAULT 'textual'"
    )
    op.execute(
        "ALTER TABLE requirement "
        "ADD COLUMN IF NOT EXISTS original_text TEXT"
    )
    op.execute(
        "ALTER TABLE requirement "
        "ADD COLUMN IF NOT EXISTS source_file VARCHAR(500)"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE requirement DROP COLUMN IF EXISTS original_text")
    op.execute("ALTER TABLE requirement DROP COLUMN IF EXISTS req_type")
    # source_file may have been added by 0006; only drop if this migration owns it
    op.execute("ALTER TABLE requirement DROP COLUMN IF EXISTS source_file")
