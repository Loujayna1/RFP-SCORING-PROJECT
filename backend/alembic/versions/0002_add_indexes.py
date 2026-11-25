"""add focused indexes for critical query patterns

Revision ID: 0002_add_indexes
Revises: 001_add_task_table
Create Date: 2026-05-03 01:00:00.000000

This migration adds ONLY indexes that target actual query patterns in the code.
Every index trades insert/update speed + storage for query speed.

Critical patterns:
1. vendor_response lookups by (rfp_id, response_id, status)
2. coverage_entry queries by response_id
3. requirement_score queries by response_id
4. vendor_score queries by response_id + rfp_id
5. task filtering by status
6. Foreign key joins
7. Audit log filtering + sorting by timestamp
"""
from alembic import op
import sqlalchemy as sa


revision = "0002_add_indexes"
down_revision = "001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ─── CRITICAL: Foreign key columns (used in every join) ───────────────────
    op.create_index("idx_vendor_response_rfp_id", "vendor_response", ["rfp_id"])
    op.create_index("idx_vendor_response_vendor_id", "vendor_response", ["vendor_id"])
    op.create_index("idx_document_rfp_id", "document", ["rfp_id"])
    op.create_index("idx_document_response_id", "document", ["response_id"])
    op.create_index("idx_doc_chunk_doc_id", "doc_chunk", ["doc_id"])
    op.create_index("idx_requirement_rfp_id", "requirement", ["rfp_id"])
    op.create_index("idx_evaluation_criterion_rfp_id", "evaluation_criterion", ["rfp_id"])
    op.create_index("idx_team_member_response_id", "team_member", ["response_id"])
    op.create_index("idx_vendor_claim_response_id", "vendor_claim", ["response_id"])
    op.create_index("idx_chat_session_rfp_id", "chat_session", ["rfp_id"])
    op.create_index("idx_chat_message_session_id", "chat_message", ["session_id"])

    # ─── CRITICAL: High-cardinality filters in hot code paths ─────────────────
    # Endpoints filter by status frequently
    op.create_index("idx_vendor_response_status", "vendor_response", ["status"])
    op.create_index("idx_task_status", "task", ["status"])

    # ─── CRITICAL: Response lookups (every evaluation query) ──────────────────
    # get_coverage, get_score, get_team all do: WHERE response_id = ?
    op.create_index("idx_coverage_entry_response_id", "coverage_entry", ["response_id"])
    op.create_index("idx_requirement_score_response_id", "requirement_score", ["response_id"])
    op.create_index("idx_vendor_score_response_id", "vendor_score", ["response_id"])

    # ─── CRITICAL: RFP lookups for comparisons ───────────────────────────────
    # compare_vendors: WHERE rfp_id = ? ORDER BY total_score
    op.create_index("idx_vendor_score_rfp_id", "vendor_score", ["rfp_id"])
    op.create_index("idx_coverage_entry_rfp_id", "coverage_entry", ["rfp_id"])


    # ─── OPTIONAL: Useful lookups for rare operations ─────────────────────────
    # Vendor name search (admin UI possible feature)
    op.create_index("idx_vendor_name", "vendor", ["name"])
    # Task retrieval by RFP (monitor progress)
    op.create_index("idx_task_rfp_id", "task", ["rfp_id"])


def downgrade() -> None:
    indexes = [
        "idx_vendor_response_rfp_id",
        "idx_vendor_response_vendor_id",
        "idx_document_rfp_id",
        "idx_document_response_id",
        "idx_doc_chunk_doc_id",
        "idx_requirement_rfp_id",
        "idx_evaluation_criterion_rfp_id",
        "idx_team_member_response_id",
        "idx_vendor_claim_response_id",
        "idx_chat_session_rfp_id",
        "idx_chat_message_session_id",
        "idx_vendor_response_status",
        "idx_task_status",
        "idx_coverage_entry_response_id",
        "idx_requirement_score_response_id",
        "idx_vendor_score_response_id",
        "idx_vendor_score_rfp_id",
        "idx_coverage_entry_rfp_id",
        "idx_vendor_name",
        "idx_task_rfp_id",
    ]
    for idx_name in indexes:
        try:
            op.drop_index(idx_name)
        except Exception:
            pass

