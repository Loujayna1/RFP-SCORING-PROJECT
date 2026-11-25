"""add report table for PDF generation tracking

Revision ID: 0004_add_report_table
Revises: 0003_add_scoring_config_and_audit_log
Create Date: 2026-05-04 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "report",
        sa.Column("report_id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("rfp_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("rfp_package.rfp_id"), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("task.task_id"), nullable=True),
        sa.Column("report_type", sa.String(100), server_default="vendor_comparison"),
        sa.Column("format", sa.String(20), server_default="pdf"),
        sa.Column("vendor_ids", postgresql.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        sa.Column("file_path", sa.String(1000), nullable=True),
        sa.Column("file_size", sa.Integer(), server_default="0"),
        sa.Column("status", sa.String(50), server_default="pending"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
    )
    op.create_index("idx_report_rfp_id", "report", ["rfp_id"])
    op.create_index("idx_report_status", "report", ["status"])
    op.create_index("idx_report_created_at", "report", ["created_at"])


def downgrade() -> None:
    op.drop_index("idx_report_created_at", table_name="report")
    op.drop_index("idx_report_status", table_name="report")
    op.drop_index("idx_report_rfp_id", table_name="report")
    op.drop_table("report")
