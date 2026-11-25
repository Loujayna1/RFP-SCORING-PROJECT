"""initial schema - all core tables with proper FK order

Revision ID: 001_initial_schema
Revises: 
Create Date: 2026-05-03 00:00:00.000000

Creates all application tables in correct dependency order:
1. RFPPackage, Vendor (root entities, no FKs)
2. Document, VendorResponse, ChatSession (FK to root)
3. DocChunk, Requirement, EvaluationCriterion, TeamMember, VendorClaim (FK to level 2)
4. OCRResult (FK to Document)
5. CoverageEntry, RequirementScore, VendorScore, ChatMessage (cross-join entities)
6. Task (references RFPPackage, VendorResponse)
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ─── Level 0: Root entities (no dependencies) ───────────────────────────
    op.create_table(
        "rfp_package",
        sa.Column("rfp_id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("issuing_company", sa.String(200)),
        sa.Column("rfp_reference", sa.String(100)),
        sa.Column("submission_deadline", sa.DateTime()),
        sa.Column("status", sa.String(50), server_default="ingesting"),
        sa.Column("requirement_count", sa.Integer(), server_default="0"),
        sa.Column("created_at", sa.DateTime()),
    )

    op.create_table(
        "vendor",
        sa.Column("vendor_id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime()),
    )

    # ─── Level 1: Entities with FK to root ───────────────────────────────────
    op.create_table(
        "document",
        sa.Column("doc_id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("rfp_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("rfp_package.rfp_id"), nullable=True),
        sa.Column("response_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("file_name", sa.String(500)),
        sa.Column("file_path", sa.String(1000)),
        sa.Column("file_type", sa.String(20)),
        sa.Column("doc_role", sa.String(50)),
        sa.Column("page_count", sa.Integer(), server_default="0"),
        sa.Column("ocr_status", sa.String(20), server_default="pending"),
        sa.Column("ingested_at", sa.DateTime()),
    )

    op.create_table(
        "vendor_response",
        sa.Column("response_id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("rfp_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("rfp_package.rfp_id")),
        sa.Column("vendor_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("vendor.vendor_id")),
        sa.Column("submitted_at", sa.DateTime()),
        sa.Column("status", sa.String(50), server_default="ingesting"),
        sa.Column("created_at", sa.DateTime()),
    )

    # Add FK from document.response_id to vendor_response
    op.create_foreign_key(
        "fk_document_response_id",
        "document", "vendor_response",
        ["response_id"], ["response_id"],
    )

    op.create_table(
        "chat_session",
        sa.Column("session_id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("rfp_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("rfp_package.rfp_id")),
        sa.Column("started_at", sa.DateTime()),
        sa.Column("last_active", sa.DateTime()),
    )

    # ─── Level 2: Entities with FK to Level 1 ───────────────────────────────
    op.create_table(
        "doc_chunk",
        sa.Column("chunk_id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("doc_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("document.doc_id")),
        sa.Column("page_number", sa.Integer()),
        sa.Column("section_heading", sa.String(500)),
        sa.Column("content", sa.Text()),
        sa.Column("char_offset_start", sa.Integer()),
        sa.Column("char_offset_end", sa.Integer()),
        sa.Column("bounding_box", postgresql.JSON()),
        sa.Column("embedding", postgresql.JSON()),
        sa.Column("created_at", sa.DateTime()),
    )

    op.create_table(
        "ocr_result",
        sa.Column("ocr_id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("doc_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("document.doc_id")),
        sa.Column("page_number", sa.Integer()),
        sa.Column("raw_text", sa.Text()),
        sa.Column("word_boxes", postgresql.JSON()),
        sa.Column("confidence_score", sa.Float()),
        sa.Column("processed_at", sa.DateTime()),
    )

    op.create_table(
        "requirement",
        sa.Column("req_id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("rfp_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("rfp_package.rfp_id")),
        sa.Column("source_doc_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("document.doc_id"), nullable=True),
        sa.Column("req_code", sa.String(50)),
        sa.Column("description", sa.Text()),
        sa.Column("category", sa.String(100)),
        sa.Column("is_mandatory", sa.Boolean(), server_default="true"),
        sa.Column("source_page", sa.Integer()),
        sa.Column("embedding", postgresql.JSON()),
    )

    op.create_table(
        "evaluation_criterion",
        sa.Column("criterion_id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("rfp_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("rfp_package.rfp_id")),
        sa.Column("label", sa.String(200)),
        sa.Column("section", sa.String(200)),
        sa.Column("weight", sa.Float()),
        sa.Column("max_score", sa.Integer(), server_default="5"),
    )

    op.create_table(
        "team_member",
        sa.Column("member_id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("response_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("vendor_response.response_id")),
        sa.Column("source_doc_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("document.doc_id"), nullable=True),
        sa.Column("full_name", sa.String(200)),
        sa.Column("role", sa.String(200)),
        sa.Column("years_experience", sa.Integer()),
        sa.Column("skills", postgresql.JSON()),
        sa.Column("certifications", postgresql.JSON()),
        sa.Column("education", postgresql.JSON()),
        sa.Column("source_page", sa.Integer()),
        sa.Column("source_bounding_box", postgresql.JSON()),
    )

    op.create_table(
        "vendor_claim",
        sa.Column("claim_id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("response_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("vendor_response.response_id")),
        sa.Column("chunk_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("doc_chunk.chunk_id"), nullable=True),
        sa.Column("claim_text", sa.Text()),
        sa.Column("claim_type", sa.String(100)),
        sa.Column("source_page", sa.Integer()),
        sa.Column("source_section", sa.String(500)),
        sa.Column("bounding_box", postgresql.JSON()),
        sa.Column("embedding", postgresql.JSON()),
    )

    op.create_table(
        "chat_message",
        sa.Column("message_id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("session_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("chat_session.session_id")),
        sa.Column("role", sa.String(20)),
        sa.Column("content", sa.Text()),
        sa.Column("retrieved_chunk_ids", postgresql.JSON()),
        sa.Column("created_at", sa.DateTime()),
    )

    # ─── Level 3: Aggregation entities ──────────────────────────────────────
    op.create_table(
        "coverage_entry",
        sa.Column("entry_id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("rfp_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("rfp_package.rfp_id")),
        sa.Column("response_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("vendor_response.response_id")),
        sa.Column("req_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("requirement.req_id")),
        sa.Column("best_claim_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("vendor_claim.claim_id"), nullable=True),
        sa.Column("similarity_score", sa.Float(), server_default="0.0"),
        sa.Column("is_covered", sa.Boolean(), server_default="false"),
        sa.Column("is_gap", sa.Boolean(), server_default="true"),
        sa.Column("evidence_text", sa.Text()),
        sa.Column("evidence_page", sa.Integer()),
        sa.Column("evidence_bounding_box", postgresql.JSON()),
        sa.Column("deep_link", sa.String(1000)),
    )

    op.create_table(
        "requirement_score",
        sa.Column("score_id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("req_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("requirement.req_id")),
        sa.Column("response_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("vendor_response.response_id")),
        sa.Column("criterion_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("evaluation_criterion.criterion_id"), nullable=True),
        sa.Column("raw_score", sa.Float(), server_default="0.0"),
        sa.Column("compliance_level", sa.String(50)),
        sa.Column("justification", sa.Text()),
        sa.Column("scored_at", sa.DateTime()),
    )

    op.create_table(
        "vendor_score",
        sa.Column("vendor_score_id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("rfp_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("rfp_package.rfp_id")),
        sa.Column("response_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("vendor_response.response_id")),
        sa.Column("total_score", sa.Float(), server_default="0.0"),
        sa.Column("section_scores", postgresql.JSON()),
        sa.Column("rank", sa.Integer()),
        sa.Column("computed_at", sa.DateTime()),
    )

    op.create_table(
        "task",
        sa.Column("task_id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("description", sa.String(500)),
        sa.Column("status", sa.String(50), index=True),
        sa.Column("progress", sa.Integer(), server_default="0"),
        sa.Column("result", postgresql.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime()),
        sa.Column("started_at", sa.DateTime()),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("rfp_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("rfp_package.rfp_id"), nullable=True),
        sa.Column("response_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("vendor_response.response_id"), nullable=True),
    )


def downgrade() -> None:
    # Drop in reverse order of creation
    op.drop_table("task")
    op.drop_table("vendor_score")
    op.drop_table("requirement_score")
    op.drop_table("coverage_entry")
    op.drop_table("chat_message")
    op.drop_table("vendor_claim")
    op.drop_table("team_member")
    op.drop_table("evaluation_criterion")
    op.drop_table("requirement")
    op.drop_table("ocr_result")
    op.drop_table("doc_chunk")
    op.drop_table("chat_session")
    op.drop_table("vendor_response")
    op.drop_table("document")
    op.drop_table("vendor")
    op.drop_table("rfp_package")
