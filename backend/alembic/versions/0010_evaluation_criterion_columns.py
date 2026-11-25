"""add missing evaluation_criterion columns (parent_id, source_file_hint)

The ORM model and persist_rfp_extraction() expect hierarchical rubric fields that
were never added in 0001. Migration 0007 added human-review columns; this fills
the remaining gaps.

Revision ID: 0010
Revises: 0009
"""
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # IF NOT EXISTS keeps this safe if columns were added manually or DB diverged.
    op.execute(
        "ALTER TABLE evaluation_criterion "
        "ADD COLUMN IF NOT EXISTS source_file_hint VARCHAR(500)"
    )
    op.execute(
        "ALTER TABLE evaluation_criterion "
        "ADD COLUMN IF NOT EXISTS parent_id UUID "
        "REFERENCES evaluation_criterion(criterion_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_evaluation_criterion_parent_id "
        "ON evaluation_criterion (parent_id)"
    )

    # Human-review columns from 0007 (in case that revision was skipped)
    op.execute(
        "ALTER TABLE evaluation_criterion "
        "ADD COLUMN IF NOT EXISTS locked_at TIMESTAMP WITHOUT TIME ZONE"
    )
    op.execute(
        "ALTER TABLE evaluation_criterion "
        "ADD COLUMN IF NOT EXISTS approved_by VARCHAR(200)"
    )
    op.execute(
        "ALTER TABLE evaluation_criterion "
        "ADD COLUMN IF NOT EXISTS confidence_score DOUBLE PRECISION"
    )
    op.execute(
        "ALTER TABLE evaluation_criterion "
        "ADD COLUMN IF NOT EXISTS extraction_source VARCHAR(50)"
    )
    op.execute(
        "ALTER TABLE evaluation_criterion "
        "ADD COLUMN IF NOT EXISTS criterion_type VARCHAR(30) DEFAULT 'scored'"
    )
    op.execute(
        "ALTER TABLE evaluation_criterion "
        "ADD COLUMN IF NOT EXISTS owner_role VARCHAR(50)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_criterion_rfp_locked "
        "ON evaluation_criterion (rfp_id, locked_at)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_evaluation_criterion_parent_id")
    op.execute("DROP INDEX IF EXISTS idx_criterion_rfp_locked")
    for col in (
        "owner_role",
        "criterion_type",
        "extraction_source",
        "confidence_score",
        "approved_by",
        "locked_at",
        "parent_id",
        "source_file_hint",
    ):
        op.execute(f"ALTER TABLE evaluation_criterion DROP COLUMN IF EXISTS {col}")
