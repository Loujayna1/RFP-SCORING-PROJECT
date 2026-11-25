"""add chat artifacts and message citations

Revision ID: 0015
Revises: 0014
Create Date: 2026-06-16 00:00:00.000000

Adds persistent chat artifacts for pinned answers / saved summaries and
stores citations alongside chat messages so exported citations survive reloads.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chat_message",
        sa.Column("citations", postgresql.JSON(astext_type=sa.Text()), nullable=True),
    )

    op.create_table(
        "chat_artifact",
        sa.Column("artifact_id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("tenant.tenant_id"), nullable=False),
        sa.Column("rfp_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("rfp_package.rfp_id"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("user.user_id"), nullable=False),
        sa.Column("artifact_type", sa.String(length=20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("citations", postgresql.JSON(astext_type=sa.Text()), nullable=True),
        sa.Column("saved_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index("idx_chat_artifact_tenant_id", "chat_artifact", ["tenant_id"])
    op.create_index("idx_chat_artifact_rfp_id", "chat_artifact", ["rfp_id"])
    op.create_index("idx_chat_artifact_user_id", "chat_artifact", ["user_id"])
    op.create_index("idx_chat_artifact_type", "chat_artifact", ["artifact_type"])


def downgrade() -> None:
    op.drop_index("idx_chat_artifact_type", table_name="chat_artifact")
    op.drop_index("idx_chat_artifact_user_id", table_name="chat_artifact")
    op.drop_index("idx_chat_artifact_rfp_id", table_name="chat_artifact")
    op.drop_index("idx_chat_artifact_tenant_id", table_name="chat_artifact")
    op.drop_table("chat_artifact")
    op.drop_column("chat_message", "citations")