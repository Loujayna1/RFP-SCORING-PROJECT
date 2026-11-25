"""add source_file to requirement

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-07 01:30:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("requirement", sa.Column("source_file", sa.String(length=500), nullable=True))


def downgrade() -> None:
    op.drop_column("requirement", "source_file")
