"""add issuing_date to rfp_package

Revision ID: 0009
Revises: 0008
"""
from alembic import op
import sqlalchemy as sa

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("rfp_package", sa.Column("issuing_date", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("rfp_package", "issuing_date")
