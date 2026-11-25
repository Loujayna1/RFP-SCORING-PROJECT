"""add human review fields for rubric locking and score override

Revision ID: 0007
Revises: 0006
Create Date: 2026-05-13 00:00:00.000000

What this adds:
  evaluation_criterion  — locked_at, approved_by, confidence_score, extraction_source
  requirement_score     — human_score, override_reason, overridden_by, overridden_at,
                          ai_score (copy of original raw_score before any override),
                          confidence_flag
  rfp_package           — rubric_locked_at, rubric_approved_by  (package-level lock)
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:

    # ── evaluation_criterion: rubric locking + extraction metadata ────────────
    op.add_column(
        "evaluation_criterion",
        sa.Column("locked_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "evaluation_criterion",
        sa.Column("approved_by", sa.String(length=200), nullable=True),
    )
    op.add_column(
        "evaluation_criterion",
        sa.Column(
            "confidence_score",
            sa.Float(),
            nullable=True,
            comment="0-1 extraction confidence. <0.75 = flagged for review",
        ),
    )
    op.add_column(
        "evaluation_criterion",
        sa.Column(
            "extraction_source",
            sa.String(length=50),
            nullable=True,
            comment="structured_table | llm_prose | manual",
        ),
    )
    op.add_column(
        "evaluation_criterion",
        sa.Column(
            "criterion_type",
            sa.String(length=30),
            server_default="scored",
            nullable=True,
            comment="scored | mandatory | preferred",
        ),
    )
    op.create_index(
        "idx_criterion_rfp_locked",
        "evaluation_criterion",
        ["rfp_id", "locked_at"],
    )

    # ── rfp_package: package-level rubric lock (convenience denorm) ───────────
    op.add_column(
        "rfp_package",
        sa.Column("rubric_locked_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "rfp_package",
        sa.Column("rubric_approved_by", sa.String(length=200), nullable=True),
    )

    # ── requirement_score: human override support ─────────────────────────────
    op.add_column(
        "requirement_score",
        sa.Column(
            "ai_score",
            sa.Float(),
            nullable=True,
            comment="Original AI-generated score, preserved even after human override",
        ),
    )
    op.add_column(
        "requirement_score",
        sa.Column(
            "human_score",
            sa.Float(),
            nullable=True,
            comment="Human override value. NULL means AI score stands.",
        ),
    )
    op.add_column(
        "requirement_score",
        sa.Column(
            "override_reason",
            sa.Text(),
            nullable=True,
            comment="Mandatory justification text when human_score is set",
        ),
    )
    op.add_column(
        "requirement_score",
        sa.Column("overridden_by", sa.String(length=200), nullable=True),
    )
    op.add_column(
        "requirement_score",
        sa.Column("overridden_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "requirement_score",
        sa.Column(
            "confidence_flag",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
            comment="True when AI confidence < threshold and human review is needed",
        ),
    )
    op.create_index(
        "idx_req_score_confidence_flag",
        "requirement_score",
        ["confidence_flag"],
    )
    op.create_index(
        "idx_req_score_response",
        "requirement_score",
        ["response_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_req_score_response", table_name="requirement_score")
    op.drop_index("idx_req_score_confidence_flag", table_name="requirement_score")
    op.drop_column("requirement_score", "confidence_flag")
    op.drop_column("requirement_score", "overridden_at")
    op.drop_column("requirement_score", "overridden_by")
    op.drop_column("requirement_score", "override_reason")
    op.drop_column("requirement_score", "human_score")
    op.drop_column("requirement_score", "ai_score")

    op.drop_column("rfp_package", "rubric_approved_by")
    op.drop_column("rfp_package", "rubric_locked_at")

    op.drop_index("idx_criterion_rfp_locked", table_name="evaluation_criterion")
    op.drop_column("evaluation_criterion", "criterion_type")
    op.drop_column("evaluation_criterion", "extraction_source")
    op.drop_column("evaluation_criterion", "confidence_score")
    op.drop_column("evaluation_criterion", "approved_by")
    op.drop_column("evaluation_criterion", "locked_at")
