"""Add evidence citation columns to requirement_score

Each requirement score keeps the exact sentence the scorer relied on, the
document and page it comes from, and a link to open it, so that every
individual score can be traced to its evidence. Nullable: scores computed
before this migration simply have no citation.

Revision ID: 0018
Revises: 0017
"""
from alembic import op

revision      = "0018"
down_revision = "0017"
branch_labels = None
depends_on    = None


def upgrade() -> None:
    op.execute("ALTER TABLE requirement_score ADD COLUMN IF NOT EXISTS evidence_quote  TEXT")
    op.execute("ALTER TABLE requirement_score ADD COLUMN IF NOT EXISTS evidence_source VARCHAR(500)")
    op.execute("ALTER TABLE requirement_score ADD COLUMN IF NOT EXISTS evidence_page   INTEGER")
    op.execute("ALTER TABLE requirement_score ADD COLUMN IF NOT EXISTS evidence_link   VARCHAR(1000)")


def downgrade() -> None:
    for col in ("evidence_link", "evidence_page", "evidence_source", "evidence_quote"):
        op.execute(f"ALTER TABLE requirement_score DROP COLUMN IF EXISTS {col}")
