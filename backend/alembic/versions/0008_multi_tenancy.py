"""0008_multi_tenancy

Adds full multi-tenant support:
  - tenant table
  - user table (replaces env-var store)
  - rfp_assignment table
  - business_vote table
  - tenant_id FK on every existing table that needs it
  - RFP status column widened to hold new state names
  - AuditLog: old_value, new_value, ip_address, user_agent columns
  - RequirementScore: evaluator_role, evaluator_user_id, is_submitted
  - EvaluationCriterion: owner_role
  - RFPPackage: winner fields, consensus_notes, locked_at/locked_by
  - PostgreSQL RLS policies on every tenant-scoped table
  - Immutable audit_log trigger (blocks UPDATE and DELETE)

Revision ID: 0008
Revises: 0007
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

# PostgreSQL reserved identifiers (e.g. "user") must be double-quoted in raw SQL.
_RESERVED_TABLES = frozenset({"user"})


def _qtable(table: str) -> str:
    if table in _RESERVED_TABLES:
        return f'"{table}"'
    return table


def upgrade() -> None:

    # ── 1. tenant ─────────────────────────────────────────────────────────────
    op.create_table(
        "tenant",
        sa.Column("tenant_id",          postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("name",               sa.String(200), nullable=False),
        sa.Column("slug",               sa.String(100), nullable=False),
        sa.Column("plan",               sa.String(30),  nullable=False, server_default="free"),
        sa.Column("status",             sa.String(30),  nullable=False, server_default="active"),
        sa.Column("max_users",          sa.Integer(),   server_default="25"),
        sa.Column("max_rfps_per_month", sa.Integer(),   server_default="10"),
        sa.Column("max_storage_gb",     sa.Integer(),   server_default="100"),
        sa.Column("billing_email",      sa.String(300), nullable=True),
        sa.Column("next_billing_date",  sa.DateTime(),  nullable=True),
        sa.Column("created_at",         sa.DateTime(),  server_default=sa.func.now()),
        sa.Column("updated_at",         sa.DateTime(),  server_default=sa.func.now()),
    )
    op.create_unique_constraint("uq_tenant_slug", "tenant", ["slug"])

    # Insert a default tenant so existing rows can be back-filled
    op.execute("""
        INSERT INTO tenant (tenant_id, name, slug, plan, status)
        VALUES ('00000000-0000-0000-0000-000000000001',
                'Default Tenant', 'default', 'pro', 'active')
        ON CONFLICT DO NOTHING
    """)

    # ── 2. user ───────────────────────────────────────────────────────────────
    op.create_table(
        "user",
        sa.Column("user_id",           postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("tenant_id",         postgresql.UUID(as_uuid=False),
                  sa.ForeignKey("tenant.tenant_id"), nullable=False),
        sa.Column("email",             sa.String(300), nullable=False),
        sa.Column("display_name",      sa.String(200), nullable=True),
        sa.Column("hashed_password",   sa.String(200), nullable=True),
        sa.Column("role",              sa.String(50),  nullable=False, server_default="technical_evaluator"),
        sa.Column("status",            sa.String(30),  nullable=False, server_default="pending"),
        sa.Column("invite_token",      sa.String(200), nullable=True),
        sa.Column("invite_expires_at", sa.DateTime(),  nullable=True),
        sa.Column("invited_by",        postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("invited_at",        sa.DateTime(),  nullable=True),
        sa.Column("accepted_at",       sa.DateTime(),  nullable=True),
        sa.Column("last_active_at",    sa.DateTime(),  nullable=True),
        sa.Column("created_at",        sa.DateTime(),  server_default=sa.func.now()),
    )
    op.create_index("ix_user_tenant_id",    "user", ["tenant_id"])
    op.create_index("ix_user_invite_token", "user", ["invite_token"])

    # ── 3. rfp_assignment ─────────────────────────────────────────────────────
    op.create_table(
        "rfp_assignment",
        sa.Column("assignment_id",  postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("rfp_id",         postgresql.UUID(as_uuid=False),
                  sa.ForeignKey("rfp_package.rfp_id"), nullable=False),
        sa.Column("user_id",        postgresql.UUID(as_uuid=False),
                  sa.ForeignKey("user.user_id"), nullable=False),
        sa.Column("tenant_id",      postgresql.UUID(as_uuid=False),
                  sa.ForeignKey("tenant.tenant_id"), nullable=False),
        sa.Column("evaluator_role", sa.String(50), nullable=False),
        sa.Column("is_submitted",   sa.Boolean(), server_default="false"),
        sa.Column("submitted_at",   sa.DateTime(), nullable=True),
        sa.Column("assigned_at",    sa.DateTime(), server_default=sa.func.now()),
        sa.Column("assigned_by",    postgresql.UUID(as_uuid=False), nullable=True),
    )
    op.create_index("ix_rfp_assignment_rfp_id",    "rfp_assignment", ["rfp_id"])
    op.create_index("ix_rfp_assignment_user_id",   "rfp_assignment", ["user_id"])
    op.create_index("ix_rfp_assignment_tenant_id", "rfp_assignment", ["tenant_id"])

    # ── 4. business_vote ──────────────────────────────────────────────────────
    op.create_table(
        "business_vote",
        sa.Column("vote_id",     postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("rfp_id",      postgresql.UUID(as_uuid=False),
                  sa.ForeignKey("rfp_package.rfp_id"), nullable=False),
        sa.Column("response_id", postgresql.UUID(as_uuid=False),
                  sa.ForeignKey("vendor_response.response_id"), nullable=False),
        sa.Column("user_id",     postgresql.UUID(as_uuid=False),
                  sa.ForeignKey("user.user_id"), nullable=False),
        sa.Column("tenant_id",   postgresql.UUID(as_uuid=False),
                  sa.ForeignKey("tenant.tenant_id"), nullable=False),
        sa.Column("vote",        sa.String(10), nullable=False),
        sa.Column("comment",     sa.Text(),     nullable=True),
        sa.Column("voted_at",    sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at",  sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index("ix_business_vote_rfp_id",    "business_vote", ["rfp_id"])
    op.create_index("ix_business_vote_tenant_id", "business_vote", ["tenant_id"])

    # ── 5. Add tenant_id to all existing tables ────────────────────────────────
    DEFAULT_TENANT = "'00000000-0000-0000-0000-000000000001'"

    tables_needing_tenant = [
        "rfp_package", "vendor", "vendor_response", "document",
        "chat_session", "task", "transaction_log", "report", "audit_log",
    ]
    for table in tables_needing_tenant:
        op.add_column(table, sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=False),
            sa.ForeignKey("tenant.tenant_id"), nullable=True,
        ))
        op.execute(f"UPDATE {table} SET tenant_id = {DEFAULT_TENANT}")
        op.alter_column(table, "tenant_id", nullable=False)
        op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])

    # ── 6. Expand RFP status column ───────────────────────────────────────────
    # Already VARCHAR(50) — new state names fit. Update default only.
    op.alter_column("rfp_package", "status", server_default="draft")

    # ── 7. RFPPackage: winner + lock fields ────────────────────────────────────
    op.add_column("rfp_package", sa.Column("created_by",         postgresql.UUID(as_uuid=False), nullable=True))
    op.add_column("rfp_package", sa.Column("winner_vendor_id",   postgresql.UUID(as_uuid=False), nullable=True))
    op.add_column("rfp_package", sa.Column("winner_declared_at", sa.DateTime(), nullable=True))
    op.add_column("rfp_package", sa.Column("winner_declared_by", postgresql.UUID(as_uuid=False), nullable=True))
    op.add_column("rfp_package", sa.Column("winner_reason",      sa.Text(), nullable=True))
    op.add_column("rfp_package", sa.Column("tie_breaker_notes",  sa.Text(), nullable=True))
    op.add_column("rfp_package", sa.Column("consensus_notes",    sa.Text(), nullable=True))
    op.add_column("rfp_package", sa.Column("locked_at",          sa.DateTime(), nullable=True))
    op.add_column("rfp_package", sa.Column("locked_by",          postgresql.UUID(as_uuid=False), nullable=True))

    # ── 8. AuditLog: provenance columns ───────────────────────────────────────
    op.add_column("audit_log", sa.Column("actor_user_id", postgresql.UUID(as_uuid=False), nullable=True))
    op.add_column("audit_log", sa.Column("old_value",     sa.JSON(), nullable=True))
    op.add_column("audit_log", sa.Column("new_value",     sa.JSON(), nullable=True))
    op.add_column("audit_log", sa.Column("ip_address",    sa.String(45), nullable=True))
    op.add_column("audit_log", sa.Column("user_agent",    sa.String(500), nullable=True))

    # ── 9. RequirementScore: evaluator fields ─────────────────────────────────
    op.add_column("requirement_score", sa.Column(
        "evaluator_role", sa.String(50), nullable=False, server_default="ai"))
    op.add_column("requirement_score", sa.Column(
        "evaluator_user_id", postgresql.UUID(as_uuid=False), nullable=True))
    op.add_column("requirement_score", sa.Column(
        "is_submitted", sa.Boolean(), nullable=False, server_default="false"))
    op.add_column("requirement_score", sa.Column(
        "submitted_at", sa.DateTime(), nullable=True))

    # ── 10. EvaluationCriterion: owner_role ───────────────────────────────────
    op.add_column("evaluation_criterion", sa.Column(
        "owner_role", sa.String(50), nullable=True))

    # ── 11. Enable RLS on every tenant-scoped table ───────────────────────────
    # RLS policies filter rows by current_setting('app.tenant_id') which the
    # FastAPI tenant middleware sets at the start of every request via:
    #   SET LOCAL app.tenant_id = '<uuid>';
    rls_tables = [
        "rfp_package", "vendor", "vendor_response", "document",
        "audit_log", "chat_session", "task", "transaction_log", "report",
        "rfp_assignment", "business_vote", "user",
    ]
    for table in rls_tables:
        qt = _qtable(table)
        op.execute(f"ALTER TABLE {qt} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {qt} FORCE ROW LEVEL SECURITY")
        op.execute(f"""
            CREATE POLICY tenant_isolation_{table}
            ON {qt}
            USING (tenant_id::text = current_setting('app.tenant_id', true))
        """)
        # Superuser bypass policy so migrations and admin tools still work
        op.execute(f"""
            CREATE POLICY superuser_bypass_{table}
            ON {qt}
            USING (current_user = 'rfp')
        """)

    # ── 12. Immutable audit_log trigger ───────────────────────────────────────
    # Raises an exception on any UPDATE or DELETE attempt.
    # Even a compromised application cannot alter history.
    op.execute("""
        CREATE OR REPLACE FUNCTION audit_log_immutable()
        RETURNS TRIGGER LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION
                'audit_log is immutable: % on row % is not permitted',
                TG_OP, OLD.audit_id;
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER trig_audit_log_immutable
        BEFORE UPDATE OR DELETE ON audit_log
        FOR EACH ROW EXECUTE FUNCTION audit_log_immutable()
    """)


def downgrade() -> None:
    # Remove trigger
    op.execute("DROP TRIGGER IF EXISTS trig_audit_log_immutable ON audit_log")
    op.execute("DROP FUNCTION IF EXISTS audit_log_immutable()")

    # Disable RLS
    rls_tables = [
        "rfp_package", "vendor", "vendor_response", "document",
        "audit_log", "chat_session", "task", "transaction_log", "report",
        "rfp_assignment", "business_vote", "user",
    ]
    for table in rls_tables:
        qt = _qtable(table)
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{table} ON {qt}")
        op.execute(f"DROP POLICY IF EXISTS superuser_bypass_{table} ON {qt}")
        op.execute(f"ALTER TABLE {qt} DISABLE ROW LEVEL SECURITY")

    # Drop added columns
    for col in ["owner_role"]:
        op.drop_column("evaluation_criterion", col)
    for col in ["evaluator_role", "evaluator_user_id", "is_submitted", "submitted_at"]:
        op.drop_column("requirement_score", col)
    for col in ["actor_user_id", "old_value", "new_value", "ip_address", "user_agent"]:
        op.drop_column("audit_log", col)
    for col in ["created_by", "winner_vendor_id", "winner_declared_at",
                "winner_declared_by", "winner_reason", "tie_breaker_notes",
                "consensus_notes", "locked_at", "locked_by"]:
        op.drop_column("rfp_package", col)
    for table in [
        "rfp_package", "vendor", "vendor_response", "document",
        "chat_session", "task", "transaction_log", "report", "audit_log",
    ]:
        op.drop_index(f"ix_{table}_tenant_id", table)
        op.drop_column(table, "tenant_id")

    op.drop_table("business_vote")
    op.drop_table("rfp_assignment")
    op.drop_table("user")
    op.drop_table("tenant")
