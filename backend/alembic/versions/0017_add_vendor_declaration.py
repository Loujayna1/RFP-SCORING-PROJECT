"""Add vendor_declaration table

Stores the compliance level declared by each vendor for each requirement, as
read from the compliance matrix it returned, together with its detailed
response and reference. The declared level is kept out of the indexed evidence
so that compliance verification cannot simply repeat the vendor's statement.

Revision ID: 0017
Revises: 0016
"""
from alembic import op

revision      = "0017"
down_revision = "0016"
branch_labels = None
depends_on    = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS vendor_declaration (
            declaration_id   UUID PRIMARY KEY,
            response_id      UUID REFERENCES vendor_response(response_id),
            req_code         VARCHAR(50),
            source_sheet     VARCHAR(200),
            source_row       INTEGER,
            requirement_text TEXT,
            declared_raw     VARCHAR(200),
            declared_level   VARCHAR(50),
            response_text    TEXT,
            reference        TEXT,
            source_file      VARCHAR(500)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_vendor_declaration_response_id "
        "ON vendor_declaration (response_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS vendor_declaration")
