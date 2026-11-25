"""add chain-of-thought reasoning column to requirement_score

Revision ID: 0014
Revises: 0013
Create Date: 2026-06-15 10:30:00.000000

Adds a `reasoning` TEXT column to `requirement_score` to store the full
chain-of-thought analysis the LLM produces before scoring each requirement.
This enables audit trails and human reviewers to understand exactly why
a particular score was assigned.
"""
from alembic import op
import sqlalchemy as sa


revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "requirement_score",
        sa.Column(
            "reasoning",
            sa.Text(),
            nullable=True,
            comment="Chain-of-thought reasoning from the LLM scoring phase",
        ),
    )


def downgrade() -> None:
    op.drop_column("requirement_score", "reasoning")
