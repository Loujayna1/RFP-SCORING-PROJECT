"""persist scoring config and add audit log

Revision ID: 0003_add_scoring_config_and_audit_log
Revises: 0002_add_indexes
Create Date: 2026-05-04 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0003"
down_revision = "0002_add_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "rfp_package",
        sa.Column("scoring_margin", sa.Integer(), server_default="10", nullable=True),
    )
    op.add_column(
        "rfp_package",
        sa.Column("compliance_levels", postgresql.JSON(), nullable=True),
    )

    op.create_table(
        "audit_log",
        sa.Column("audit_id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("rfp_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("rfp_package.rfp_id"), nullable=True),
        sa.Column("entity_type", sa.String(100), nullable=False),
        sa.Column("entity_id", sa.String(100), nullable=True),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("actor", sa.String(100), server_default="system"),
        sa.Column("details", postgresql.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("idx_audit_log_rfp_id", "audit_log", ["rfp_id"])
    op.create_index("idx_audit_log_action", "audit_log", ["action"])
    op.create_index("idx_audit_log_timestamp", "audit_log", ["created_at"])


def downgrade() -> None:
    op.drop_index("idx_audit_log_timestamp", table_name="audit_log")
    op.drop_index("idx_audit_log_action", table_name="audit_log")
    op.drop_index("idx_audit_log_rfp_id", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_column("rfp_package", "compliance_levels")
    op.drop_column("rfp_package", "scoring_margin")