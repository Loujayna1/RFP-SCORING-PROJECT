"""
app/models/db.py
────────────────────────────────────────────────────────────────────────────
Multi-tenant schema. Every tenant-scoped table carries tenant_id so that
PostgreSQL Row-Level Security (RLS) can enforce isolation at the DB layer.

NEW IN THIS VERSION (migration 0008)
─────────────────────────────────────
1. Tenant          — top-level org record
2. User            — replaces env-var user store; scoped to tenant
3. RFPAssignment   — links a user+role to a specific RFP
4. BusinessVote    — yes/no/maybe from business representatives
5. tenant_id FK    — added to RFPPackage, Vendor, VendorResponse, AuditLog,
                     ChatSession, Task, Report, TransactionLog, Document
6. RFP status      — expanded to 7-state machine with VALID_TRANSITIONS map
7. AuditLog        — added old_value, new_value, ip_address, user_agent
8. RequirementScore — added evaluator_role, evaluator_user_id, is_submitted
9. EvaluationCriterion — added owner_role (controls per-role visibility)
"""

import uuid
import enum
from datetime import datetime
from sqlalchemy import (
    Column, String, Integer, Float, Boolean,
    DateTime, ForeignKey, Text, JSON,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


def new_uuid():
    return str(uuid.uuid4())


# ── Enums (string-valued for easy serialisation) ──────────────────────────────

class RFPStatus(str, enum.Enum):
    DRAFT                  = "draft"
    CRITERIA_APPROVED      = "criteria_approved"
    VENDORS_UPLOADED       = "vendors_uploaded"
    AI_EVAL_COMPLETE       = "ai_eval_complete"
    HUMAN_EVAL_IN_PROGRESS = "human_eval_in_progress"
    CONSENSUS              = "consensus"
    LOCKED                 = "locked"
    # Legacy compat
    INGESTING              = "ingesting"
    READY                  = "ready"
    SCORING                = "scoring"
    ERROR                  = "error"


VALID_TRANSITIONS: dict[str, list[str]] = {
    RFPStatus.DRAFT:                  [RFPStatus.CRITERIA_APPROVED],
    RFPStatus.CRITERIA_APPROVED:      [RFPStatus.VENDORS_UPLOADED],
    RFPStatus.VENDORS_UPLOADED:       [RFPStatus.AI_EVAL_COMPLETE],
    RFPStatus.AI_EVAL_COMPLETE:       [RFPStatus.HUMAN_EVAL_IN_PROGRESS],
    RFPStatus.HUMAN_EVAL_IN_PROGRESS: [RFPStatus.CONSENSUS],
    RFPStatus.CONSENSUS:              [RFPStatus.LOCKED],
    RFPStatus.LOCKED:                 [],
    RFPStatus.INGESTING:              [RFPStatus.READY, RFPStatus.ERROR, RFPStatus.DRAFT],
    RFPStatus.READY:                  [RFPStatus.CRITERIA_APPROVED, RFPStatus.VENDORS_UPLOADED],
    RFPStatus.SCORING:                [RFPStatus.AI_EVAL_COMPLETE, RFPStatus.ERROR],
    RFPStatus.ERROR:                  [RFPStatus.DRAFT, RFPStatus.INGESTING],
}


class UserRole(str, enum.Enum):
    ADMIN                = "admin"
    TECHNICAL_EVALUATOR  = "technical_evaluator"
    FINANCIAL_EVALUATOR  = "financial_evaluator"
    OPERATIONS_EVALUATOR = "operations_evaluator"
    LEGAL_COMPLIANCE     = "legal_compliance"
    BUSINESS_REP         = "business_rep"
    AI_AUDITOR           = "ai_auditor"


class UserStatus(str, enum.Enum):
    PENDING     = "pending"
    ACTIVE      = "active"
    DEACTIVATED = "deactivated"


class TenantPlan(str, enum.Enum):
    FREE       = "free"
    PRO        = "pro"
    ENTERPRISE = "enterprise"


# ── Tenant ────────────────────────────────────────────────────────────────────

class Tenant(Base):
    __tablename__ = "tenant"
    tenant_id         = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    name              = Column(String(200), nullable=False)
    slug              = Column(String(100), unique=True, nullable=False)
    plan              = Column(String(30), default=TenantPlan.FREE)
    status            = Column(String(30), default="active")
    max_users         = Column(Integer, default=25)
    max_rfps_per_month = Column(Integer, default=10)
    max_storage_gb    = Column(Integer, default=100)
    billing_email     = Column(String(300), nullable=True)
    next_billing_date = Column(DateTime, nullable=True)
    created_at        = Column(DateTime, default=datetime.utcnow)
    updated_at        = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    users = relationship("User", back_populates="tenant", cascade="all, delete-orphan")
    rfps  = relationship("RFPPackage", back_populates="tenant")


# ── User (replaces env-var store) ─────────────────────────────────────────────

class User(Base):
    __tablename__ = "user"
    user_id           = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    tenant_id         = Column(UUID(as_uuid=False), ForeignKey("tenant.tenant_id"), nullable=False, index=True)
    email             = Column(String(300), nullable=False)
    display_name      = Column(String(200), nullable=True)
    hashed_password   = Column(String(200), nullable=True)
    role              = Column(String(50), default=UserRole.TECHNICAL_EVALUATOR)
    status            = Column(String(30), default=UserStatus.PENDING)
    invite_token      = Column(String(200), nullable=True, index=True)
    invite_expires_at = Column(DateTime, nullable=True)
    invited_by        = Column(UUID(as_uuid=False), ForeignKey("user.user_id"), nullable=True)
    invited_at        = Column(DateTime, nullable=True)
    accepted_at       = Column(DateTime, nullable=True)
    last_active_at    = Column(DateTime, nullable=True)
    created_at        = Column(DateTime, default=datetime.utcnow)

    tenant      = relationship("Tenant", back_populates="users")
    assignments = relationship("RFPAssignment", back_populates="user",
                               foreign_keys="RFPAssignment.user_id")


# ── Per-RFP role assignment ───────────────────────────────────────────────────

class RFPAssignment(Base):
    __tablename__ = "rfp_assignment"
    assignment_id  = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    rfp_id         = Column(UUID(as_uuid=False), ForeignKey("rfp_package.rfp_id"), nullable=False, index=True)
    user_id        = Column(UUID(as_uuid=False), ForeignKey("user.user_id"), nullable=False, index=True)
    tenant_id      = Column(UUID(as_uuid=False), ForeignKey("tenant.tenant_id"), nullable=False, index=True)
    evaluator_role = Column(String(50), nullable=False)
    is_submitted   = Column(Boolean, default=False)
    submitted_at   = Column(DateTime, nullable=True)
    assigned_at    = Column(DateTime, default=datetime.utcnow)
    assigned_by    = Column(UUID(as_uuid=False), ForeignKey("user.user_id"), nullable=True)

    user = relationship("User", back_populates="assignments", foreign_keys=[user_id])
    rfp  = relationship("RFPPackage", back_populates="assignments")


# ── Business vote ─────────────────────────────────────────────────────────────

class BusinessVote(Base):
    __tablename__ = "business_vote"
    vote_id     = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    rfp_id      = Column(UUID(as_uuid=False), ForeignKey("rfp_package.rfp_id"), nullable=False, index=True)
    response_id = Column(UUID(as_uuid=False), ForeignKey("vendor_response.response_id"), nullable=False)
    user_id     = Column(UUID(as_uuid=False), ForeignKey("user.user_id"), nullable=False)
    tenant_id   = Column(UUID(as_uuid=False), ForeignKey("tenant.tenant_id"), nullable=False, index=True)
    vote        = Column(String(10), nullable=False)  # yes | no | maybe
    comment     = Column(Text, nullable=True)
    voted_at    = Column(DateTime, default=datetime.utcnow)
    updated_at  = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ── RFP Package ───────────────────────────────────────────────────────────────

class RFPPackage(Base):
    __tablename__ = "rfp_package"
    rfp_id              = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    tenant_id           = Column(UUID(as_uuid=False), ForeignKey("tenant.tenant_id"), nullable=False, index=True)
    title               = Column(String(500), nullable=False)
    issuing_company     = Column(String(200))
    issuing_date        = Column(DateTime)
    rfp_reference       = Column(String(100))
    submission_deadline = Column(DateTime)
    status              = Column(String(50), default=RFPStatus.DRAFT)
    requirement_count   = Column(Integer, default=0)
    scoring_margin      = Column(Integer, default=10)
    compliance_levels   = Column(JSON, default=list)
    created_at          = Column(DateTime, default=datetime.utcnow)
    created_by          = Column(UUID(as_uuid=False), ForeignKey("user.user_id"), nullable=True)

    # Rubric lock
    rubric_locked_at    = Column(DateTime, nullable=True)
    rubric_approved_by  = Column(String(200), nullable=True)

    # Winner declaration
    winner_vendor_id    = Column(UUID(as_uuid=False), ForeignKey("vendor.vendor_id"), nullable=True)
    winner_declared_at  = Column(DateTime, nullable=True)
    winner_declared_by  = Column(UUID(as_uuid=False), ForeignKey("user.user_id"), nullable=True)
    winner_reason       = Column(Text, nullable=True)
    tie_breaker_notes   = Column(Text, nullable=True)

    # Consensus + lock
    consensus_notes     = Column(Text, nullable=True)
    locked_at           = Column(DateTime, nullable=True)
    locked_by           = Column(UUID(as_uuid=False), ForeignKey("user.user_id"), nullable=True)

    tenant       = relationship("Tenant", back_populates="rfps")
    documents    = relationship("Document", back_populates="rfp")
    requirements = relationship("Requirement", back_populates="rfp")
    criteria     = relationship("EvaluationCriterion", back_populates="rfp")
    responses    = relationship("VendorResponse", back_populates="rfp")
    assignments  = relationship("RFPAssignment", back_populates="rfp")


# ── Vendor ────────────────────────────────────────────────────────────────────

class Vendor(Base):
    __tablename__ = "vendor"
    vendor_id  = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    tenant_id  = Column(UUID(as_uuid=False), ForeignKey("tenant.tenant_id"), nullable=False, index=True)
    name       = Column(String(200), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    responses  = relationship("VendorResponse", back_populates="vendor")


# ── Vendor Response ───────────────────────────────────────────────────────────

class VendorResponse(Base):
    __tablename__ = "vendor_response"
    response_id  = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    rfp_id       = Column(UUID(as_uuid=False), ForeignKey("rfp_package.rfp_id"))
    vendor_id    = Column(UUID(as_uuid=False), ForeignKey("vendor.vendor_id"))
    tenant_id    = Column(UUID(as_uuid=False), ForeignKey("tenant.tenant_id"), nullable=False, index=True)
    submitted_at = Column(DateTime)
    status       = Column(String(50), default="ingesting")
    created_at   = Column(DateTime, default=datetime.utcnow)

    rfp       = relationship("RFPPackage", back_populates="responses")
    vendor    = relationship("Vendor", back_populates="responses")
    documents = relationship("Document", back_populates="response")
    claims    = relationship("VendorClaim", back_populates="response")
    team      = relationship("TeamMember", back_populates="response")


# ── Requirement Score (+ evaluator fields) ────────────────────────────────────

class RequirementScore(Base):
    __tablename__ = "requirement_score"
    score_id          = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    req_id            = Column(UUID(as_uuid=False), ForeignKey("requirement.req_id"))
    response_id       = Column(UUID(as_uuid=False), ForeignKey("vendor_response.response_id"))
    criterion_id      = Column(UUID(as_uuid=False), ForeignKey("evaluation_criterion.criterion_id"), nullable=True)
    raw_score         = Column(Float, default=0.0)
    compliance_level  = Column(String(50))
    justification     = Column(Text)
    reasoning         = Column(Text, nullable=True, comment="Chain-of-thought reasoning from the LLM scoring phase")
    scored_at         = Column(DateTime, default=datetime.utcnow)
    ai_score          = Column(Float, nullable=True)
    human_score       = Column(Float, nullable=True)
    override_reason   = Column(Text, nullable=True)
    overridden_by     = Column(String(200), nullable=True)
    overridden_at     = Column(DateTime, nullable=True)
    confidence_flag   = Column(Boolean, default=False, nullable=False)
    # Evidence the score relies on (exact sentence, document, page, link)
    evidence_quote    = Column(Text, nullable=True)
    evidence_source   = Column(String(500), nullable=True)
    evidence_page     = Column(Integer, nullable=True)
    evidence_link     = Column(String(1000), nullable=True)
    # Multi-role fields
    evaluator_role    = Column(String(50), default="ai", nullable=False)
    evaluator_user_id = Column(UUID(as_uuid=False), ForeignKey("user.user_id"), nullable=True)
    is_submitted      = Column(Boolean, default=False, nullable=False)
    submitted_at      = Column(DateTime, nullable=True)


# ── Audit Log (immutable, hardened) ──────────────────────────────────────────

class AuditLog(Base):
    __tablename__ = "audit_log"
    audit_id      = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    tenant_id     = Column(UUID(as_uuid=False), ForeignKey("tenant.tenant_id"), nullable=True, index=True)
    rfp_id        = Column(UUID(as_uuid=False), ForeignKey("rfp_package.rfp_id"), nullable=True, index=True)
    entity_type   = Column(String(100), nullable=False)
    entity_id     = Column(String(100), nullable=True)
    action        = Column(String(100), nullable=False, index=True)
    actor         = Column(String(100), default="system")
    actor_user_id = Column(UUID(as_uuid=False), ForeignKey("user.user_id"), nullable=True)
    old_value     = Column(JSON, nullable=True)
    new_value     = Column(JSON, nullable=True)
    details       = Column(JSON, default=dict)
    ip_address    = Column(String(45), nullable=True)
    user_agent    = Column(String(500), nullable=True)
    created_at    = Column(DateTime, default=datetime.utcnow, index=True)


# ── Evaluation Criterion (+ owner_role) ───────────────────────────────────────

class EvaluationCriterion(Base):
    __tablename__ = "evaluation_criterion"
    criterion_id      = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    rfp_id            = Column(UUID(as_uuid=False), ForeignKey("rfp_package.rfp_id"))
    label             = Column(String(200))
    section           = Column(String(200))
    weight            = Column(Float)
    max_score         = Column(Integer, default=5)
    source_file_hint  = Column(String(500), nullable=True)
    parent_id         = Column(UUID(as_uuid=False), ForeignKey("evaluation_criterion.criterion_id"), nullable=True)
    criterion_type    = Column(String(30), default="scored", nullable=True)
    confidence_score  = Column(Float, nullable=True)
    extraction_source = Column(String(50), nullable=True)
    locked_at         = Column(DateTime, nullable=True)
    approved_by       = Column(String(200), nullable=True)
    # NULL = all roles can see; set to a UserRole value to restrict
    owner_role        = Column(String(50), nullable=True)
    # Drives which scorer is used during vendor evaluation.
    # NULL = unclassified (falls back to legacy direct scorer).
    evaluation_mode   = Column(String(50), nullable=True)
    rfp = relationship("RFPPackage", back_populates="criteria")


# ── Unchanged tables ──────────────────────────────────────────────────────────

class Document(Base):
    __tablename__ = "document"
    doc_id      = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    rfp_id      = Column(UUID(as_uuid=False), ForeignKey("rfp_package.rfp_id"), nullable=True)
    response_id = Column(UUID(as_uuid=False), ForeignKey("vendor_response.response_id"), nullable=True)
    tenant_id   = Column(UUID(as_uuid=False), ForeignKey("tenant.tenant_id"), nullable=True, index=True)
    file_name   = Column(String(500))
    file_path   = Column(String(1000))
    file_type   = Column(String(20))
    doc_role    = Column(String(50))
    page_count  = Column(Integer, default=0)
    ocr_status  = Column(String(20), default="pending")
    ingested_at = Column(DateTime, default=datetime.utcnow)
    rfp      = relationship("RFPPackage", back_populates="documents")
    response = relationship("VendorResponse", back_populates="documents")
    chunks   = relationship("DocChunk", back_populates="document")


class DocChunk(Base):
    __tablename__ = "doc_chunk"
    chunk_id          = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    doc_id            = Column(UUID(as_uuid=False), ForeignKey("document.doc_id"))
    page_number       = Column(Integer)
    section_heading   = Column(String(500))
    content           = Column(Text)
    char_offset_start = Column(Integer)
    char_offset_end   = Column(Integer)
    bounding_box      = Column(JSON)
    embedding         = Column(JSON)
    created_at        = Column(DateTime, default=datetime.utcnow)
    document = relationship("Document", back_populates="chunks")


class OCRResult(Base):
    __tablename__ = "ocr_result"
    ocr_id           = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    doc_id           = Column(UUID(as_uuid=False), ForeignKey("document.doc_id"))
    page_number      = Column(Integer)
    raw_text         = Column(Text)
    word_boxes       = Column(JSON)
    confidence_score = Column(Float)
    processed_at     = Column(DateTime, default=datetime.utcnow)


class TeamMember(Base):
    __tablename__ = "team_member"
    member_id           = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    response_id         = Column(UUID(as_uuid=False), ForeignKey("vendor_response.response_id"))
    source_doc_id       = Column(UUID(as_uuid=False), ForeignKey("document.doc_id"), nullable=True)
    full_name           = Column(String(200))
    role                = Column(String(200))
    years_experience    = Column(Integer)
    skills              = Column(JSON, default=list)
    certifications      = Column(JSON, default=list)
    education           = Column(JSON, default=list)
    source_page         = Column(Integer)
    source_bounding_box = Column(JSON)
    response = relationship("VendorResponse", back_populates="team")


class Requirement(Base):
    __tablename__ = "requirement"
    req_id        = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    rfp_id        = Column(UUID(as_uuid=False), ForeignKey("rfp_package.rfp_id"))
    source_doc_id = Column(UUID(as_uuid=False), ForeignKey("document.doc_id"), nullable=True)
    req_code      = Column(String(50))
    description   = Column(Text)
    category      = Column(String(100))
    is_mandatory  = Column(Boolean, default=True)
    source_page   = Column(Integer)
    source_file   = Column(String(500), nullable=True)
    source_sheet       = Column(String(200), nullable=True)
    matrix_row_order   = Column(Integer, nullable=True)
    matrix_sheet_order = Column(Integer, nullable=True)
    req_type      = Column(String(20), default="textual")
    original_text = Column(Text, nullable=True)
    embedding     = Column(JSON)
    rfp = relationship("RFPPackage", back_populates="requirements")


class VendorClaim(Base):
    __tablename__ = "vendor_claim"
    claim_id       = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    response_id    = Column(UUID(as_uuid=False), ForeignKey("vendor_response.response_id"))
    chunk_id       = Column(UUID(as_uuid=False), ForeignKey("doc_chunk.chunk_id"), nullable=True)
    claim_text     = Column(Text)
    claim_type     = Column(String(100))
    source_page    = Column(Integer)
    source_section = Column(String(500))
    bounding_box   = Column(JSON)
    embedding      = Column(JSON)
    response = relationship("VendorResponse", back_populates="claims")


class VendorDeclaration(Base):
    """
    Compliance level declared by a vendor for a requirement, read from the
    compliance matrix it returned. Kept apart from the evidence so that the
    verification never sees the vendor's own statement.
    """
    __tablename__ = "vendor_declaration"
    declaration_id   = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    response_id      = Column(UUID(as_uuid=False), ForeignKey("vendor_response.response_id"), index=True)
    req_code         = Column(String(50))
    source_sheet     = Column(String(200))
    source_row       = Column(Integer)
    requirement_text = Column(Text)
    declared_raw     = Column(String(200))
    declared_level   = Column(String(50), nullable=True)
    response_text    = Column(Text)
    reference        = Column(Text)
    source_file      = Column(String(500))


class CoverageEntry(Base):
    __tablename__ = "coverage_entry"
    entry_id              = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    rfp_id                = Column(UUID(as_uuid=False), ForeignKey("rfp_package.rfp_id"))
    response_id           = Column(UUID(as_uuid=False), ForeignKey("vendor_response.response_id"))
    req_id                = Column(UUID(as_uuid=False), ForeignKey("requirement.req_id"))
    best_claim_id         = Column(UUID(as_uuid=False), ForeignKey("vendor_claim.claim_id"), nullable=True)
    similarity_score      = Column(Float, default=0.0)
    is_covered            = Column(Boolean, default=False)
    is_gap                = Column(Boolean, default=True)
    evidence_text         = Column(Text)
    evidence_page         = Column(Integer)
    evidence_bounding_box = Column(JSON)
    deep_link             = Column(String(1000))


class VendorScore(Base):
    __tablename__ = "vendor_score"
    vendor_score_id = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    rfp_id          = Column(UUID(as_uuid=False), ForeignKey("rfp_package.rfp_id"))
    response_id     = Column(UUID(as_uuid=False), ForeignKey("vendor_response.response_id"))
    total_score     = Column(Float, default=0.0)
    section_scores  = Column(JSON, default=dict)
    rank            = Column(Integer)
    computed_at     = Column(DateTime, default=datetime.utcnow)


class ChatSession(Base):
    __tablename__ = "chat_session"
    session_id  = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    rfp_id      = Column(UUID(as_uuid=False), ForeignKey("rfp_package.rfp_id"))
    tenant_id   = Column(UUID(as_uuid=False), ForeignKey("tenant.tenant_id"), nullable=True, index=True)
    started_at  = Column(DateTime, default=datetime.utcnow)
    last_active = Column(DateTime, default=datetime.utcnow)
    messages    = relationship("ChatMessage", back_populates="session")


class ChatMessage(Base):
    __tablename__ = "chat_message"
    message_id          = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    session_id          = Column(UUID(as_uuid=False), ForeignKey("chat_session.session_id"))
    role                = Column(String(20))
    content             = Column(Text)
    retrieved_chunk_ids = Column(JSON, default=list)
    citations           = Column(JSON, default=list)
    created_at          = Column(DateTime, default=datetime.utcnow)
    session = relationship("ChatSession", back_populates="messages")


class ChatArtifact(Base):
    __tablename__ = "chat_artifact"
    artifact_id   = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    tenant_id     = Column(UUID(as_uuid=False), ForeignKey("tenant.tenant_id"), nullable=False, index=True)
    rfp_id        = Column(UUID(as_uuid=False), ForeignKey("rfp_package.rfp_id"), nullable=False, index=True)
    user_id       = Column(UUID(as_uuid=False), ForeignKey("user.user_id"), nullable=False, index=True)
    artifact_type = Column(String(20), nullable=False)
    content       = Column(Text, nullable=False)
    citations     = Column(JSON, default=list)
    saved_at      = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at    = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Task(Base):
    __tablename__ = "task"
    task_id      = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    tenant_id    = Column(UUID(as_uuid=False), ForeignKey("tenant.tenant_id"), nullable=True, index=True)
    description  = Column(String(500))
    status       = Column(String(50), index=True)
    progress     = Column(Integer, default=0)
    result       = Column(JSON, nullable=True)
    error        = Column(Text, nullable=True)
    created_at   = Column(DateTime, default=datetime.utcnow)
    started_at   = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)
    rfp_id       = Column(UUID(as_uuid=False), ForeignKey("rfp_package.rfp_id"), nullable=True)
    response_id  = Column(UUID(as_uuid=False), ForeignKey("vendor_response.response_id"), nullable=True)


class TransactionLog(Base):
    __tablename__ = "transaction_log"
    txn_id             = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    tenant_id          = Column(UUID(as_uuid=False), ForeignKey("tenant.tenant_id"), nullable=True, index=True)
    entity_type        = Column(String(100), nullable=False, index=True)
    entity_id          = Column(String(100), nullable=False)
    operation          = Column(String(50), nullable=False)
    status             = Column(String(50), default="pending")
    milvus_collections = Column(JSON, default=list)
    db_tables_modified = Column(JSON, default=list)
    created_at         = Column(DateTime, default=datetime.utcnow, index=True)
    completed_at       = Column(DateTime, nullable=True)
    error_message      = Column(Text, nullable=True)
    compensated_at     = Column(DateTime, nullable=True)


class Report(Base):
    __tablename__ = "report"
    report_id     = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    rfp_id        = Column(UUID(as_uuid=False), ForeignKey("rfp_package.rfp_id"), nullable=False, index=True)
    tenant_id     = Column(UUID(as_uuid=False), ForeignKey("tenant.tenant_id"), nullable=True, index=True)
    task_id       = Column(UUID(as_uuid=False), ForeignKey("task.task_id"), nullable=True)
    report_type   = Column(String(100), default="vendor_comparison")
    format        = Column(String(20), default="pdf")
    vendor_ids    = Column(JSON, default=list)
    file_path     = Column(String(1000), nullable=True)
    file_size     = Column(Integer, default=0)
    status        = Column(String(50), default="pending")
    error_message = Column(Text, nullable=True)
    created_at    = Column(DateTime, default=datetime.utcnow, index=True)
    completed_at  = Column(DateTime, nullable=True)
    task = relationship("Task", foreign_keys=[task_id])


