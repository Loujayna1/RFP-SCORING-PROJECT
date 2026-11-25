"""
Mode-specific direct criterion scorers.

Each scorer:
  1. Retrieves vendor evidence from Milvus using mode-specific query expansion.
  2. Calls the appropriate LLM scoring function from llm.py.
  3. Returns a normalized result dict (criterion_id, score, evidence, etc.).

Dispatcher
----------
  SCORER_BY_MODE maps evaluation_mode → scorer coroutine.
  score_direct_criteria() loops over non-requirement-coverage criteria and
  dispatches each one.

The requirement_coverage mode is handled by the existing requirement scoring
pipeline in evaluation.py and is intentionally NOT dispatched here.
"""

import asyncio
import logging
from typing import Optional, Callable, Awaitable

from app.core.aioutil import loop_semaphore
from app.services.vector_store import get_vector_store, col_claims, col_vendor_chunks, col_cv_skills
from app.services.reranker import rerank_hits
from app.services.llm import score_generic_criterion, score_team_capability_criterion

logger = logging.getLogger(__name__)

# Per-event-loop semaphores (see app.core.aioutil.loop_semaphore): the Celery
# worker runs each task in its own asyncio loop, so a process-global Semaphore
# crashes on the second task. Limits unchanged.
_MILVUS_SEM_LIMIT = 20
_LLM_SEM_LIMIT    = 5


def _milvus_sem() -> asyncio.Semaphore:
    return loop_semaphore("crit_milvus", _MILVUS_SEM_LIMIT)


def _llm_sem() -> asyncio.Semaphore:
    return loop_semaphore("crit_llm", _LLM_SEM_LIMIT)

# ── Mode-specific search expansions ──────────────────────────────────────────
# Each mode gets a set of extra query terms to improve retrieval recall.
_MODE_QUERY_EXPANSIONS: dict[str, list[str]] = {
    "plan_methodology": [
        "project plan", "implementation plan", "methodology",
        "roadmap", "timeline", "milestones", "deliverables",
        "governance", "rollout", "deployment approach",
    ],
    "experience_references": [
        "references", "portfolio", "similar project", "case study",
        "customer", "prior implementation", "sector experience",
        "comparable deployment", "track record",
    ],
    "team_capability": [
        "team", "resources", "staff", "personnel", "consultants",
        "certifications", "experience", "CVs", "proposed team",
    ],
    "solution_quality": [
        "proposal quality", "solution", "architecture", "clarity",
        "completeness", "innovation", "executive summary",
    ],
    "commercial": [
        "pricing", "cost", "financial proposal", "commercial offer",
        "TCO", "licensing", "fee structure",
    ],
}


# ── Evidence retrieval helpers ────────────────────────────────────────────────

async def _search_evidence(
    rfp_id: str,
    response_id: str,
    query: str,
    top_k_candidates: int = 10,
    top_k_reranked: int = 5,
) -> list[dict]:
    """Search claims then vendor_chunks as fallback, rerank, return top hits."""
    vs          = get_vector_store()
    claims_col  = col_claims(rfp_id)
    vchunks_col = col_vendor_chunks(rfp_id)
    filter_expr = f'response_id == "{response_id}"'

    async def _search(col_name: str) -> list[dict]:
        async with _milvus_sem():
            hits = await asyncio.to_thread(vs.search, col_name, query, top_k_candidates, filter_expr)
        if not hits:
            return []
        return await asyncio.to_thread(rerank_hits, query, hits, top_k_reranked)

    hits = await _search(claims_col)
    if not hits or hits[0]["score"] < 0.10:
        hits = await _search(vchunks_col)
    return hits


def _hits_to_evidence_text(hits: list[dict]) -> tuple[str, float]:
    """Format hits into combined evidence text + best similarity score."""
    if not hits:
        return "", 0.0
    chunks = []
    for idx, hit in enumerate(hits):
        meta     = hit.get("metadata", {})
        doc_info = f"Document: {meta.get('file_name', 'Unknown')}, Page: {meta.get('page_number', 1)}"
        chunks.append(f"[Chunk {idx+1} | {doc_info} | Score: {hit['score']:.4f}]\n{hit['text']}")
    return "\n\n".join(chunks), hits[0]["score"]


async def _get_team_summary(rfp_id: str, response_id: str) -> dict:
    """
    Load structured TeamMember rows from the DB and format a summary dict.
    Imports DB models locally to avoid circular dependencies.
    """
    try:
        from app.db.session import AsyncSessionLocal
        from app.models import db as models_db
        from sqlalchemy import select

        async with AsyncSessionLocal() as db:
            rows = (
                await db.execute(
                    select(models_db.TeamMember)
                    .where(models_db.TeamMember.response_id == response_id)
                )
            ).scalars().all()

        if not rows:
            return {"member_count": 0, "roles": [], "avg_years_experience": 0.0, "certifications": [], "members": []}

        all_certs = []
        total_exp = 0
        roles     = []
        members   = []
        for m in rows:
            roles.append(m.role or "Unknown")
            yrs = m.years_experience or 0
            total_exp += yrs
            certs = m.certifications or []
            all_certs.extend(certs)
            members.append({
                "full_name":        m.full_name or "Unknown",
                "role":             m.role or "Unknown",
                "years_experience": yrs,
                "certifications":   certs,
            })

        avg_exp = total_exp / len(rows) if rows else 0.0
        return {
            "member_count":         len(rows),
            "roles":                list(set(roles)),
            "avg_years_experience": round(avg_exp, 1),
            "certifications":       list(set(all_certs)),
            "members":              members,
        }
    except Exception as exc:
        logger.warning("_get_team_summary failed for response %s: %s", response_id, exc)
        return {"member_count": 0, "roles": [], "avg_years_experience": 0.0, "certifications": [], "members": []}


# ── Individual mode scorers ───────────────────────────────────────────────────

def _make_deep_link(hits: list[dict]) -> Optional[str]:
    if not hits:
        return None
    meta   = hits[0].get("metadata", {})
    doc_id = meta.get("doc_id", meta.get("file_name", "unknown"))
    page   = meta.get("page_number", 1)
    return f"/docs/{doc_id}/view?page={page}&type=criterion"


def _build_result(
    criterion: dict,
    llm_result: dict,
    hits: list[dict],
    score_source: str,
) -> dict:
    """Build the normalized criterion score result dict."""
    meta    = hits[0].get("metadata", {}) if hits else {}
    page    = meta.get("page_number", 1)
    source  = meta.get("file_name", "")
    sim     = hits[0]["score"] if hits else 0.0

    evidence_excerpts = [
        {
            "text":         hit["text"][:300],
            "page_number":  hit.get("metadata", {}).get("page_number", 1),
            "source":       hit.get("metadata", {}).get("file_name", ""),
            "score":        round(hit["score"], 4),
        }
        for hit in hits[:5]
    ]

    return {
        # Primary keys for aggregation
        "criterion_id":     criterion.get("criterion_id"),
        "req_id":           None,
        "req_code":         criterion.get("label", "")[:30],
        "description":      criterion.get("label", ""),
        "category":         criterion.get("label", ""),

        # Scores
        "raw_score":        llm_result["score"],
        "compliance_level": llm_result["compliance_level"],
        "justification":    llm_result["justification"],
        "reasoning":        llm_result["reasoning"],
        "confidence":       llm_result["confidence"],

        # Source metadata
        "score_source":     score_source,
        "evaluation_mode":  criterion.get("evaluation_mode"),

        # Evidence for UI
        "evidence":         evidence_excerpts,
        "citation": {
            "evidence_excerpt": (hits[0]["text"][:300] if hits else ""),
            "page_number":      page,
            "deep_link":        _make_deep_link(hits) or "#",
            "source":           source,
            "similarity_score": round(sim, 4),
        },
    }


async def score_plan_methodology_criterion(rfp_id: str, response_id: str, criterion: dict) -> Optional[dict]:
    label  = criterion.get("label", "")
    parent = criterion.get("section", "")

    # Build a rich query from label + parent + mode expansions
    expansions = _MODE_QUERY_EXPANSIONS["plan_methodology"]
    # Use the top 3 most contextually relevant expansion terms for the query
    query = f"{label} {parent} {' '.join(expansions[:3])}".strip()

    hits = await _search_evidence(rfp_id, response_id, query)
    evidence_text, sim = _hits_to_evidence_text(hits)

    async with _llm_sem():
        llm_result = await score_generic_criterion(
            criterion_label=label,
            parent_label=parent or None,
            evaluation_mode="plan_methodology",
            evidence_text=evidence_text or None,
            similarity_score=sim,
        )

    return _build_result(criterion, llm_result, hits, "direct_criterion")


async def score_experience_references_criterion(rfp_id: str, response_id: str, criterion: dict) -> Optional[dict]:
    label  = criterion.get("label", "")
    parent = criterion.get("section", "")

    expansions = _MODE_QUERY_EXPANSIONS["experience_references"]
    query = f"{label} {parent} {' '.join(expansions[:3])}".strip()

    hits = await _search_evidence(rfp_id, response_id, query)
    evidence_text, sim = _hits_to_evidence_text(hits)

    async with _llm_sem():
        llm_result = await score_generic_criterion(
            criterion_label=label,
            parent_label=parent or None,
            evaluation_mode="experience_references",
            evidence_text=evidence_text or None,
            similarity_score=sim,
        )

    return _build_result(criterion, llm_result, hits, "direct_criterion")


async def score_team_capability_criterion_fn(rfp_id: str, response_id: str, criterion: dict) -> Optional[dict]:
    label  = criterion.get("label", "")
    parent = criterion.get("section", "")

    # Prefer structured DB data; also retrieve unstructured evidence for context
    team_summary = await _get_team_summary(rfp_id, response_id)

    expansions = _MODE_QUERY_EXPANSIONS["team_capability"]
    query = f"{label} {parent} {' '.join(expansions[:3])}".strip()
    hits  = await _search_evidence(rfp_id, response_id, query, top_k_candidates=10, top_k_reranked=3)
    evidence_text, _ = _hits_to_evidence_text(hits)

    async with _llm_sem():
        llm_result = await score_team_capability_criterion(
            criterion_label=label,
            parent_label=parent or None,
            team_summary=team_summary,
            evidence_text=evidence_text or None,
        )

    return _build_result(criterion, llm_result, hits, "direct_criterion")


async def score_solution_quality_criterion(rfp_id: str, response_id: str, criterion: dict) -> Optional[dict]:
    label  = criterion.get("label", "")
    parent = criterion.get("section", "")

    expansions = _MODE_QUERY_EXPANSIONS["solution_quality"]
    query = f"{label} {parent} {' '.join(expansions[:3])}".strip()

    hits = await _search_evidence(rfp_id, response_id, query)
    evidence_text, sim = _hits_to_evidence_text(hits)

    async with _llm_sem():
        llm_result = await score_generic_criterion(
            criterion_label=label,
            parent_label=parent or None,
            evaluation_mode="solution_quality",
            evidence_text=evidence_text or None,
            similarity_score=sim,
        )

    return _build_result(criterion, llm_result, hits, "direct_criterion")


async def score_commercial_criterion(rfp_id: str, response_id: str, criterion: dict) -> Optional[dict]:
    label  = criterion.get("label", "")
    parent = criterion.get("section", "")

    # Commercial evidence tends to live in pricing/financial docs
    expansions = _MODE_QUERY_EXPANSIONS["commercial"]
    query = f"{label} {parent} {' '.join(expansions[:3])}".strip()

    hits = await _search_evidence(rfp_id, response_id, query)
    evidence_text, sim = _hits_to_evidence_text(hits)

    async with _llm_sem():
        llm_result = await score_generic_criterion(
            criterion_label=label,
            parent_label=parent or None,
            evaluation_mode="commercial",
            evidence_text=evidence_text or None,
            similarity_score=sim,
        )

    return _build_result(criterion, llm_result, hits, "direct_criterion")


# ── Dispatcher ────────────────────────────────────────────────────────────────

# Maps evaluation_mode → scorer coroutine
SCORER_BY_MODE: dict[str, Callable] = {
    "plan_methodology":      score_plan_methodology_criterion,
    "experience_references": score_experience_references_criterion,
    "team_capability":       score_team_capability_criterion_fn,
    "solution_quality":      score_solution_quality_criterion,
    "commercial":            score_commercial_criterion,
}


async def score_direct_criteria(
    rfp_id: str,
    response_id: str,
    criteria: list[dict],
) -> list[dict]:
    """
    Score all leaf criteria whose evaluation_mode is NOT requirement_coverage.

    Criteria with evaluation_mode=None (legacy/unclassified) are skipped here
    and handled by the old _score_criterion_directly fallback in evaluation.py.
    """
    direct_criteria = [
        c for c in criteria
        if c.get("criterion_type") not in ("parent",)
        and c.get("evaluation_mode") not in (None, "requirement_coverage")
    ]

    if not direct_criteria:
        return []

    logger.info(
        "Scoring %d direct criteria for response %s",
        len(direct_criteria), response_id,
    )

    async def _score_one(criterion: dict) -> Optional[dict]:
        mode    = criterion.get("evaluation_mode", "")
        scorer  = SCORER_BY_MODE.get(mode)
        if scorer is None:
            logger.warning("No scorer for evaluation_mode=%r, skipping criterion '%s'", mode, criterion.get("label"))
            return None
        try:
            return await scorer(rfp_id, response_id, criterion)
        except Exception as exc:
            logger.error(
                "Direct scorer failed for criterion '%s' (mode=%s): %s",
                criterion.get("label"), mode, exc,
            )
            return None

    results = await asyncio.gather(*[_score_one(c) for c in direct_criteria])
    return [r for r in results if r is not None]
