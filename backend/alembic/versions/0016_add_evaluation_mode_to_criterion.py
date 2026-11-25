"""Add evaluation_mode column to evaluation_criterion

Each leaf criterion is now classified into a scoring mode that drives which
evaluator runs during vendor pipeline (requirement_coverage, plan_methodology,
experience_references, team_capability, solution_quality, commercial).
NULL = unclassified / legacy (backward-compatible default).

Revision ID: 0016
Revises: 0015
"""
from alembic import op

revision     = "0016"
down_revision = "0015"
branch_labels = None
depends_on    = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE evaluation_criterion "
        "ADD COLUMN IF NOT EXISTS evaluation_mode VARCHAR(50)"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE evaluation_criterion "
        "DROP COLUMN IF EXISTS evaluation_mode"
    )
