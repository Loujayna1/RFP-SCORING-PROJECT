"""add transaction log table

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-06 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "transaction_log",
        sa.Column("txn_id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("entity_type", sa.String(100), nullable=False),
        sa.Column("entity_id", sa.String(100), nullable=False),
        sa.Column("operation", sa.String(50), nullable=False),
        sa.Column("status", sa.String(50), server_default="pending"),
        sa.Column("milvus_collections", postgresql.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        sa.Column("db_tables_modified", postgresql.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("compensated_at", sa.DateTime(), nullable=True),
    )
    op.create_index("idx_transaction_log_entity_type", "transaction_log", ["entity_type"])
    op.create_index("idx_transaction_log_created_at", "transaction_log", ["created_at"])


def downgrade() -> None:
    op.drop_index("idx_transaction_log_created_at", table_name="transaction_log")
    op.drop_index("idx_transaction_log_entity_type", table_name="transaction_log")
    op.drop_table("transaction_log")
