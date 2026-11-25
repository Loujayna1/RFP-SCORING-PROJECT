"""
Core evaluation pipeline — wired to Milvus vector search.

Hybrid matrix-aware flow
------------------------
1. For each requirement, semantic-search the vendor claims collection.
2. Build CoverageEntry per requirement (is_covered, similarity_score,
   evidence, deep_link).
3. LLM CoT-scores each requirement using retrieved evidence.
4. For criteria classified as requirement_coverage, aggregate
   requirement scores into criterion-level scores.
5. For all other leaf criteria (plan_methodology, experience_references,
   team_capability, solution_quality, commercial), call the appropriate
   mode-specific scorer from criterion_scorers.py.
6. Criteria with evaluation_mode=None (legacy / unclassified) fall back
   to the old _score_criterion_directly path.
7. Weighted aggregation → VendorScore using criterion-level scores.

Backward compatibility
----------------------
Existing RFPs whose EvaluationCriterion rows have evaluation_mode=NULL
continue to work exactly as before: requirement coverage scoring runs,
unmatched criteria are scored directly with the legacy scorer.
"""
import asyncio
import logging
import re
from typing import Optional

from app.core.config import settings
from app.core.aioutil import loop_semaphore
from app.services.vector_store import get_vector_store, col_claims, col_vendor_chunks
from app.services.llm import score_requirement
from app.services.reranker import rerank_hits

logger = logging.getLogger(__name__)

COVERAGE_THRESHOLD = settings.COVERAGE_THRESHOLD   # cosine >= this → covered

# Limit concurrent Milvus + LLM calls so we don't burst rate limits.
# Created per running event loop (see loop_semaphore) because the Celery worker
# runs each task in its own asyncio loop; a process-global Semaphore crashes on
# the second task with "bound to a different event loop". Limits unchanged.
_MILVUS_SEM_LIMIT = 20
_LLM_SEM_LIMIT    = 5


def _milvus_sem() -> asyncio.Semaphore:
    return loop_semaphore("eval_milvus", _MILVUS_SEM_LIMIT)


def _llm_sem() -> asyncio.Semaphore:
    return loop_semaphore("eval_llm", _LLM_SEM_LIMIT)


def build_deep_link(doc_id: str, page_number: int, bbox: Optional[dict] = None, doc_type: str = "claim") -> str:
    bbox_str = ""
    if bbox and isinstance(bbox, dict):
        bbox_str = f"&bbox={bbox.get('x',0)},{bbox.get('y',0)},{bbox.get('w',100)},{bbox.get('h',20)}"
    return f"/docs/{doc_id}/view?page={page_number}{bbox_str}&type={doc_type}"


# ── Per-requirement Milvus search ─────────────────────────────────────────────
def _evidence_collections(claims_col: str, vchunks_col: str) -> list[str]:
    """Collections searched for evidence, from settings.EVIDENCE_SOURCE."""
    source = (getattr(settings, "EVIDENCE_SOURCE", "chunks") or "chunks").lower()
    if source == "claims":
        return [claims_col]
    if source == "both":
        return [vchunks_col, claims_col]
    return [vchunks_col]


async def _search_one_requirement(
    req: dict,
    vs,
    claims_col: str,
    filter_expr: str,
    fallback_col: Optional[str] = None,
) -> dict:
    """
    Retrieve the passages of the vendor's documents that bear on one requirement.

    Hybrid search (dense + BM25, fused by RRF) runs on every evidence
    collection selected by settings.EVIDENCE_SOURCE; the union is de-duplicated
    and reranked by the cross-encoder, and the best EVIDENCE_TOP_K passages are
    numbered [Chunk 1..k] for the scorer. Each passage keeps its own document,
    page and link so the passage the scorer relies on can be cited exactly.
    """
    top_k = int(getattr(settings, "EVIDENCE_TOP_K", 5) or 5)
    collections = _evidence_collections(claims_col, fallback_col or claims_col)

    hits: list[dict] = []
    for col in collections:
        async with _milvus_sem():
            found = await asyncio.to_thread(vs.search, col, req["description"], 10, filter_expr)
        hits.extend(found or [])

    # Child hits are expanded to their parent passage, so several hits can
    # carry the same text: keep one of each before reranking.
    unique: list[dict] = []
    seen: set[str] = set()
    for h in hits:
        key = (h.get("text") or "").strip()[:500]
        if key and key not in seen:
            seen.add(key)
            unique.append(h)
    if getattr(settings, "RERANK_ENABLED", True):
        reranked_hits = (
            await asyncio.to_thread(rerank_hits, req["description"], unique, top_k) if unique else []
        )
    else:
        # Ablation: keep the retrieval order (RRF / dense / BM25) without the
        # cross-encoder. Scores are then retrieval scores, so the coverage and
        # review-flag thresholds are not meaningful in this mode.
        reranked_hits = unique[:top_k]

    req_id = req["req_id"]

    if not reranked_hits:
        return {
            "req_id":                req_id,
            "req_code":              req.get("req_code", req_id),
            "description":           req["description"],
            "best_claim_id":         None,
            "similarity_score":      0.0,
            "is_covered":            False,
            "is_gap":                True,
            "evidence_text":         None,
            "evidence_page":         None,
            "evidence_bounding_box": None,
            "deep_link":             None,
            "source":                None,
            "vendor_name":           "",
            "passages":              [],
        }

    best = reranked_hits[0]
    sim  = best["score"]
    meta = best["metadata"]

    is_covered = sim >= COVERAGE_THRESHOLD
    is_gap     = sim < 0.25

    page      = meta.get("page_number", 1)
    bbox      = meta.get("bounding_box")
    doc_id    = meta.get("doc_id", meta.get("file_name", "unknown"))
    deep_link = meta.get("deep_link") or build_deep_link(doc_id, page, bbox)

    passages = []
    evidence_chunks = []
    for idx, hit in enumerate(reranked_hits, start=1):
        m = hit["metadata"]
        p_page = m.get("page_number", 1)
        passages.append({
            "chunk":     idx,
            "text":      hit["text"],
            "file_name": m.get("file_name", ""),
            "page":      p_page,
            "deep_link": m.get("deep_link") or build_deep_link(
                m.get("doc_id", m.get("file_name", "unknown")), p_page, m.get("bounding_box")
            ),
            "score":     round(hit["score"], 4),
        })
        evidence_chunks.append(
            f"[Chunk {idx} | Document: {m.get('file_name', 'Unknown')} | Page: {p_page}]\n{hit['text']}"
        )
    evidence_text = "\n\n".join(evidence_chunks)

    return {
        "req_id":                req_id,
        "req_code":              req.get("req_code", req_id),
        "description":           req["description"],
        "best_claim_id":         best["id"],   # kept only if it is a real claim (checked at persistence)
        "similarity_score":      round(sim, 4),
        "is_covered":            is_covered,
        "is_gap":                is_gap,
        "evidence_text":         evidence_text,
        "evidence_page":         page,
        "evidence_bounding_box": bbox,
        "deep_link":             deep_link,
        "source":                meta.get("file_name", ""),
        "vendor_name":           meta.get("vendor_name", ""),
        "passages":              passages,
        "top_hits": [
            {"score": round(h["score"], 4), "text": h["text"][:200]}
            for h in reranked_hits
        ],
    }


async def build_coverage_matrix(
    rfp_id: str,
    response_id: str,
    requirements: list[dict],
) -> dict:
    """
    For each requirement, run a Milvus COSINE search against the vendor claims
    collection filtered to this specific response_id.
    All searches run concurrently, bounded by _MILVUS_SEM = 20.
    """
    vs          = get_vector_store()
    claims_col  = col_claims(rfp_id)
    vchunks_col = col_vendor_chunks(rfp_id)
    filter_expr = f'response_id == "{response_id}"'

    entries = await asyncio.gather(*[
        _search_one_requirement(req, vs, claims_col, filter_expr, vchunks_col)
        for req in requirements
    ])

    covered_count = sum(1 for e in entries if e["is_covered"])
    gap_count     = sum(1 for e in entries if e["is_gap"] and not e["is_covered"])
    partial_count = sum(1 for e in entries if not e["is_covered"] and not e["is_gap"])
    total         = len(requirements)
    coverage_rate = covered_count / total if total else 0.0

    return {
        "rfp_id":               rfp_id,
        "response_id":          response_id,
        "coverage_rate":        round(coverage_rate, 4),
        "total_requirements":   total,
        "covered_count":        covered_count,
        "partial_count":        partial_count,
        "gap_count":            gap_count,
        "entries":              list(entries),
    }


# ── LLM scoring of requirements ───────────────────────────────────────────────
def _normalise_ws(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def resolve_evidence(result: dict, passages: list[dict]) -> Optional[dict]:
    """
    Turn the scorer's choice of passage into a citation.

    The scorer names the chunk it relied on and copies the decisive sentence.
    The quote is checked against the passages: the passage that actually
    contains it wins (so a wrong chunk number cannot produce a wrong page);
    otherwise the chunk number given by the scorer is used. A NotAddressed
    verdict has no evidence by definition.
    """
    if not passages or result.get("compliance_level") in ("NotAddressed", "ScoringFailed"):
        return None
    quote = (result.get("evidence_quote") or "").strip()
    chosen = None
    quote_verified = False
    if quote:
        q = _normalise_ws(quote)[:120]
        for p in passages:
            if q and q in _normalise_ws(p["text"]):
                chosen, quote_verified = p, True
                break
    if chosen is None:
        try:
            n = int(result.get("evidence_chunk") or 0)
        except (TypeError, ValueError):
            n = 0
        chosen = next((p for p in passages if p["chunk"] == n), None)
    if chosen is None:
        return None
    return {
        "quote":          quote or None,
        "quote_verified": quote_verified,
        "source":         chosen["file_name"],
        "page":           chosen["page"],
        "deep_link":      chosen["deep_link"],
        "chunk":          chosen["chunk"],
        "passage":        chosen["text"][:1500],
    }


async def _score_one_requirement(req: dict, cov: dict, compliance_levels: Optional[list[dict]] = None) -> dict:
    async with _llm_sem():
        result = await score_requirement(
            requirement_description=req["description"],
            req_code=req.get("req_code", req["req_id"]),
            evidence_text=cov.get("evidence_text"),
            similarity_score=cov.get("similarity_score", 0.0),
            compliance_levels=compliance_levels,
        )

    evidence = resolve_evidence(result, cov.get("passages") or [])

    citation = None
    if evidence:
        citation = {
            "evidence_excerpt": evidence["quote"] or evidence["passage"][:300],
            "page_number":      evidence["page"],
            "deep_link":        evidence["deep_link"],
            "source":           evidence["source"],
            "similarity_score": cov.get("similarity_score", 0.0),
        }

    return {
        "req_id":           req["req_id"],
        "req_code":         req.get("req_code", req["req_id"]),
        "description":      req["description"],
        "category":         req.get("category", "General"),
        "raw_score":        result["raw_score"],
        "compliance_level": result["compliance_level"],
        "justification":    result["justification"],
        "reasoning":        result.get("reasoning", ""),
        "citation":         citation,
        "evidence":         evidence,
        "no_evidence":      bool(result.get("no_evidence")),
        "scoring_failed":   bool(result.get("scoring_failed")),
    }


async def score_all_requirements(
    requirements: list[dict],
    coverage_entries: list[dict],
    compliance_levels: Optional[list[dict]] = None,
) -> list[dict]:
    """
    LLM CoT-score every requirement concurrently.
    _LLM_SEM(5) prevents Groq rate-limit bursts.
    """
    cov_by_req = {e["req_id"]: e for e in coverage_entries}
    tasks = [
        _score_one_requirement(req, cov_by_req.get(req["req_id"], {}), compliance_levels)
        for req in requirements
    ]
    return list(await asyncio.gather(*tasks))


# ── Legacy direct scorer (evaluation_mode=None fallback) ──────────────────────
def _fuzzy_match(query: str, options: dict[str, str]) -> Optional[str]:
    """
    Word-level fuzzy match when exact category key not found.
    Handles "operational" → "1.5. operation requirements" etc.
    """
    if not query:
        return None
    import re as _re
    def _words(s: str) -> set[str]:
        return {w for w in _re.split(r"[\W_]+", s.lower()) if len(w) > 3}

    q_words = _words(query)
    if not q_words:
        return None

    best_label: Optional[str] = None
    best_overlap = 0

    for key, label in options.items():
        if query in key or key in query:
            return label
        k_words = _words(key)
        overlap = sum(
            1 for qw in q_words
            for kw in k_words
            if qw.startswith(kw[:5]) or kw.startswith(qw[:5])
        )
        if overlap > best_overlap:
            best_overlap = overlap
            best_label = label

    return best_label if best_overlap > 0 else None


async def _score_criterion_directly(
    rfp_id: str,
    response_id: str,
    criterion: dict,
) -> Optional[dict]:
    """
    Legacy fallback: score an unclassified criterion (evaluation_mode=None)
    from vendor evidence using the requirement scoring prompt.

    Returns a requirement-score-shaped dict with req_id=None (FK is nullable).
    """
    label        = criterion.get("label", "")
    criterion_id = criterion.get("criterion_id")
    if not label or not criterion_id:
        return None

    vs          = get_vector_store()
    claims_col  = col_claims(rfp_id)
    vchunks_col = col_vendor_chunks(rfp_id)
    filter_expr = f'response_id == "{response_id}"'

    section      = criterion.get("section", "")
    search_query = (
        f"{label} {section}".strip()
        if section and section.lower() != label.lower()
        else label
    )

    async def _search(col_name: str) -> list[dict]:
        async with _milvus_sem():
            hits = await asyncio.to_thread(vs.search, col_name, search_query, 10, filter_expr)
        if not hits:
            return []
        return await asyncio.to_thread(rerank_hits, search_query, hits, 5)

    hits = await _search(claims_col)
    if not hits or hits[0]["score"] < 0.10:
        hits = await _search(vchunks_col)

    if not hits:
        logger.info("No vendor evidence for legacy criterion '%s' — skipping", label)
        return None

    evidence_chunks = []
    for idx, hit in enumerate(hits):
        meta     = hit["metadata"]
        doc_info = f"Document: {meta.get('file_name', 'Unknown')}, Page: {meta.get('page_number', 1)}"
        evidence_chunks.append(
            f"[Chunk {idx+1} | Source: {doc_info} | Score: {hit['score']:.4f}]\n{hit['text']}"
        )
    evidence_text = "\n\n".join(evidence_chunks)
    sim           = hits[0]["score"]

    async with _llm_sem():
        result = await score_requirement(
            requirement_description=label,
            req_code=label[:30],
            evidence_text=evidence_text,
            similarity_score=sim,
        )

    meta      = hits[0]["metadata"]
    page      = meta.get("page_number", 1)
    bbox      = meta.get("bounding_box")
    doc_id    = meta.get("doc_id", meta.get("file_name", "unknown"))
    deep_link = build_deep_link(doc_id, page, bbox)

    return {
        "req_id":           None,
        "req_code":         label[:30],
        "description":      label,
        "category":         label,
        "criterion_id":     criterion_id,
        "raw_score":        result["raw_score"],
        "compliance_level": result["compliance_level"],
        "justification":    result["justification"],
        "reasoning":        result.get("reasoning", ""),
        "citation": {
            "evidence_excerpt":  evidence_text[:300],
            "page_number":       page,
            "deep_link":         deep_link,
            "source":            meta.get("file_name", ""),
            "similarity_score":  round(sim, 4),
        },
    }


# ── Requirement → criterion mapping ──────────────────────────────────────────
def _norm_label(text: str) -> str:
    """'1.2. Architecture & Integration' -> 'architecture and integration'."""
    t = re.sub(r"^\s*\d+(?:\.\d+)*\.?\s*", "", str(text or ""))
    t = t.lower().replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def map_requirements_to_criteria(requirements: list[dict], criteria: list[dict]) -> dict[str, tuple]:
    """
    Assign every requirement to one LEAF criterion of the evaluation grid.

    The category of a matrix requirement is the name of the sheet it comes
    from; the evaluation grid normally has one sub-criterion per sheet with the
    same name. Matching is tried in this order, on normalised labels:
      exact     — same label (or section) once codes and punctuation are removed
      contains  — one label contains the other ("Security" / "Security Requirements")
      fuzzy     — word overlap (the previous behaviour), recorded as such
    Parent criteria are never targets: their score comes from their children.

    Returns {req_id: (criterion_id, method)}; requirements that match nothing
    are absent and reported, never silently put in an "Other" bucket.
    """
    leaves, _ = _select_leaf_criteria(criteria)
    leaves = [c for c in leaves if c.get("criterion_id")]
    by_norm: dict[str, str] = {}
    for c in leaves:
        for key in (c.get("label"), c.get("section")):
            n = _norm_label(key)
            if n:
                by_norm.setdefault(n, c["criterion_id"])

    mapping: dict[str, tuple] = {}
    unmatched: dict[str, int] = {}
    for r in requirements:
        cat = _norm_label(r.get("category") or r.get("source_sheet") or "")
        cid, method = None, None
        if cat in by_norm:
            cid, method = by_norm[cat], "exact"
        elif cat:
            contains = [v for k, v in by_norm.items() if k and (k in cat or cat in k)]
            if len(set(contains)) == 1:
                cid, method = contains[0], "contains"
            else:
                label = _fuzzy_match(cat, {k: v for k, v in by_norm.items()})
                if label:
                    cid, method = label, "fuzzy"
        if cid:
            mapping[r["req_id"]] = (cid, method)
        else:
            unmatched[r.get("category") or "?"] = unmatched.get(r.get("category") or "?", 0) + 1
    if unmatched:
        logger.warning("Requirements with no matching criterion (category: count): %s", unmatched)
    fuzzy = sum(1 for _, m in mapping.values() if m == "fuzzy")
    if fuzzy:
        logger.warning("%d requirements mapped to a criterion by fuzzy word overlap only", fuzzy)
    return mapping


# ── Hierarchy helpers ─────────────────────────────────────────────────────────

_LEADING_CODE_RE = re.compile(r"^\s*(\d+(?:\.\d+)*)")


def _criterion_code(c: dict) -> Optional[str]:
    """
    Extract a hierarchical code like '1' or '1.2' from a criterion.

    Prefers an explicit '_code'/'code' field; otherwise reads the leading
    numeric code off the label/section (e.g. "1.2.Submission Quality" → "1.2").
    Returns None when no code can be found.
    """
    for key in ("_code", "code"):
        v = c.get(key)
        if v and _LEADING_CODE_RE.match(str(v).strip()):
            return str(v).strip()
    for key in ("label", "section"):
        m = _LEADING_CODE_RE.match(str(c.get(key) or ""))
        if m:
            return m.group(1)
    return None


def _select_leaf_criteria(criteria: list[dict]) -> tuple[list[dict], list[dict]]:
    """
    Split criteria into (leaves, parents) by STRUCTURE, independent of the
    stored ``criterion_type`` — which upstream extraction paths do not reliably
    set (the flat matrix path saves every row, parents included, as "scored").

    A criterion is treated as a parent (non-leaf) when any of these holds:
      1. it is explicitly tagged ``criterion_type == "parent"``; or
      2. its ``criterion_id`` is the ``parent_id`` of another criterion; or
      3. its hierarchical code is a strict prefix of another's (``"1"`` is a
         parent because ``"1.2"`` exists).

    Only leaf criteria carry real weights into the weighted total; counting a
    parent as a leaf double-counts its children (bug #2).
    """
    parent_ids = {c.get("parent_id") for c in criteria if c.get("parent_id")}
    all_codes = {code for c in criteria if (code := _criterion_code(c))}

    leaves: list[dict] = []
    parents: list[dict] = []
    for c in criteria:
        code = _criterion_code(c)
        is_parent = (
            c.get("criterion_type") == "parent"
            or (c.get("criterion_id") and c.get("criterion_id") in parent_ids)
            or (code is not None and any(o != code and o.startswith(code + ".") for o in all_codes))
        )
        (parents if is_parent else leaves).append(c)
    return leaves, parents


# ── Criterion-level aggregation ───────────────────────────────────────────────


def compute_effective_weights(criteria: list[dict]) -> dict[str, float]:
    """
    Effective weight of every criterion, as a share of the total (sums to 1
    over the leaves).

    A criterion receives its parent's share, divided among its siblings in
    proportion to their weights:

        eff(root)  = w(root)  / sum(w of all roots)
        eff(child) = eff(parent) * w(child) / sum(w of its siblings)

    This single rule covers both conventions found in evaluation matrices:
    relative sub-weights that sum to 1 within each parent (a parent at 0.7 with
    children at 0.35 / 0.35 / 0.3 gives 0.245 / 0.245 / 0.21), and absolute
    sub-weights that sum to the parent's weight (40 split as 25 / 15 gives
    0.25 / 0.15 of a total of 100). It reproduces the SUMPRODUCT formulas used
    in the company's own evaluation spreadsheets. Mandatory (pass/fail)
    criteria carry no weight.
    """
    by_id = {c.get("criterion_id"): c for c in criteria if c.get("criterion_id")}

    def _w(c: dict) -> float:
        if (c.get("criterion_type") or "scored") == "mandatory":
            return 0.0
        try:
            return max(float(c.get("weight") or 0.0), 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _parent(c: dict) -> Optional[str]:
        pid = c.get("parent_id")
        return pid if pid in by_id else None

    siblings: dict[Optional[str], list[dict]] = {}
    for c in criteria:
        siblings.setdefault(_parent(c), []).append(c)

    eff: dict[str, float] = {}

    def _visit(parent_id: Optional[str], parent_share: float) -> None:
        group = siblings.get(parent_id, [])
        total = sum(_w(c) for c in group)
        for c in group:
            share = parent_share * (_w(c) / total) if total > 0 else 0.0
            cid = c.get("criterion_id")
            if cid:
                eff[cid] = share
                _visit(cid, share)

    _visit(None, 1.0)
    return eff

def aggregate_vendor_score(
    requirement_scores: list[dict],
    criteria: list[dict],
    direct_criterion_scores: Optional[list[dict]] = None,
) -> dict:
    """
    Compute the final vendor score from criterion-level scores.

    Flow
    ----
    1. Average requirement scores per criterion → criterion score for
       requirement_coverage criteria.
    2. Merge with direct_criterion_scores (modes != requirement_coverage).
    3. section_scores dict: criterion_label → score (for UI + VendorScore.section_scores).
    4. Weighted total = Σ(criterion_score × effective_weight) / Σ(effective_weight),
       where the effective weight of a leaf is its share of the total once the
       weights of its parents are applied (see compute_effective_weights).

    Backward compatibility
    ----------------------
    * direct_criterion_scores defaults to None (existing callers unchanged).
    * Criteria with criterion_type='parent' are excluded from the weighted total.
    * NaN / zero-weight guards carried over from original implementation.

    Hierarchical weights note
    -------------------------
    Leaf weights are combined with the weights of their parents. Using leaf
    weights alone gives every main criterion the same influence when sub-weights
    are relative (each group summing to 1), instead of the influence the matrix
    assigns to it. Criteria without a parent keep their own weight.
    """
    leaf_criteria, _parent_criteria = _select_leaf_criteria(criteria)
    effective = compute_effective_weights(criteria)

    # ── Step 1: Build criterion_id → score from requirement coverage ──────────
    # Build two lookups: by criterion_id and by label (for legacy fuzzy match)
    crit_by_id:    dict[str, dict] = {c["criterion_id"]: c for c in leaf_criteria if c.get("criterion_id")}
    cat_map_label: dict[str, str]  = {}
    cat_map_id:    dict[str, str]  = {}
    for c in leaf_criteria:
        cid = c.get("criterion_id", "")
        for key in [c.get("label", "").lower(), c.get("section", "").lower()]:
            if key:
                cat_map_label[key] = c.get("label", "")
                if cid:
                    cat_map_id[key] = cid

    # Bucket requirement scores by criterion_id
    req_buckets_by_cid:   dict[str, list[float]] = {}
    req_buckets_by_label: dict[str, list[float]] = {}   # fallback when criterion_id unknown

    for s in requirement_scores:
        if s.get("human_score") is None and s.get("compliance_level") == "ScoringFailed":
            continue   # no assessment was produced: no score until a human reviews it
        final = (
            s["human_score"]
            if s.get("human_score") is not None
            else s.get("raw_score", 0.0)
        )
        # Use explicit criterion_id if present (set during run_full_evaluation)
        cid = s.get("criterion_id")
        if cid and cid in crit_by_id:
            req_buckets_by_cid.setdefault(cid, []).append(float(final))
        else:
            # Fuzzy fallback: map category → criterion label
            cat   = s.get("category", "").lower()
            label = cat_map_label.get(cat) or _fuzzy_match(cat, cat_map_label) or "Other"
            req_buckets_by_label.setdefault(label, []).append(float(final))

    # Criterion score from requirements = mean of constituent requirement scores
    criterion_scores: dict[str, float] = {}

    for cid, vals in req_buckets_by_cid.items():
        label = crit_by_id[cid].get("label", cid)
        criterion_scores[cid] = round(sum(vals) / len(vals), 3)

    # Also handle label-bucketed scores (backward compat)
    for label, vals in req_buckets_by_label.items():
        if label == "Other":
            continue
        # Find matching criterion by label
        for c in leaf_criteria:
            if c.get("label", "") == label and c.get("criterion_id"):
                cid = c["criterion_id"]
                if cid not in criterion_scores:
                    criterion_scores[cid] = round(sum(vals) / len(vals), 3)
                break

    # ── Step 2: Merge direct criterion scores (override by criterion_id) ──────
    direct_scores = direct_criterion_scores or []
    for ds in direct_scores:
        cid = ds.get("criterion_id")
        if cid and cid not in req_buckets_by_cid:
            # raw_score (0-5 from CoT scale) or score (0-5 from generic scorer).
            # A criterion scored from its requirements keeps that score.
            val = ds.get("score") if ds.get("score") is not None else ds.get("raw_score", 0.0)
            criterion_scores[cid] = round(float(val), 3)

    # ── Step 3: Build section_scores for UI / VendorScore persistence ─────────
    section_scores: dict[str, float] = {}

    # Include requirement-coverage buckets under their labels
    for label, vals in req_buckets_by_label.items():
        section_scores[label] = round(sum(vals) / len(vals), 3)

    # Include all criterion-level scores
    for c in leaf_criteria:
        cid   = c.get("criterion_id", "")
        label = c.get("label", "")
        if cid and cid in criterion_scores:
            section_scores[label] = criterion_scores[cid]

    # ── Step 4: Weighted total ────────────────────────────────────────────────
    # A leaf with NO requirement score and NO direct score was never actually
    # evaluated (retrieval/mapping gap, not a vendor failure). Charging it 0 in
    # the weighted total is a false negative (bug #5), so such criteria are
    # excluded from BOTH numerator and denominator and surfaced separately for
    # manual scoring. A genuine 0 (NonCompliant / NotAddressed) still counts.
    total      = 0.0
    weight_sum = 0.0
    unscored_criteria: list[dict] = []
    for c in leaf_criteria:
        cid    = c.get("criterion_id", "")
        label  = c.get("label", "")
        weight = effective.get(cid) if cid in effective else float(c.get("weight") or 0.0)

        # Mandatory criteria are pass/fail gates, not weighted contributors —
        # exclude them from the weighted total (consistent across the grading,
        # override, and consensus paths).
        if (c.get("criterion_type") or "scored") == "mandatory":
            continue

        if cid and cid in criterion_scores:
            score = criterion_scores[cid]
        elif label in section_scores:
            score = section_scores[label]
        else:
            # No evidence mapped to this criterion — do not auto-zero it.
            unscored_criteria.append({
                "criterion_id": cid or None,
                "label":        label,
                "weight":       weight,
                "reason":       "insufficient_evidence",
            })
            continue

        total      += score * weight
        weight_sum += weight

    if weight_sum > 0:
        total = total / weight_sum
    elif section_scores:
        vals  = [v for v in section_scores.values()]
        total = sum(vals) / len(vals)
    else:
        total = 0.0

    if total != total or total in (float("inf"), float("-inf")):
        logger.warning("aggregate_vendor_score produced non-finite total; defaulting to 0.0")
        total = 0.0

    if unscored_criteria:
        logger.info(
            "aggregate_vendor_score: %d leaf criteria had no evidence and were "
            "excluded from the weighted total (surfaced for manual scoring): %s",
            len(unscored_criteria),
            ", ".join(u["label"] for u in unscored_criteria),
        )

    return {
        "total_score":       round(total, 3),
        "section_scores":    section_scores,
        "unscored_criteria": unscored_criteria,
        "criterion_scores":  criterion_scores,
        "weight_sum":        round(weight_sum, 6),
    }


def recompute_total_from_rows(score_rows, criterion_rows) -> dict:
    """
    Recompute a vendor's total from ORM rows using the SAME aggregation the
    grading pipeline uses (aggregate_vendor_score). The human-review and
    consensus override paths call this so their totals can never diverge from
    the graded value — one source of truth for parent handling, no-evidence
    exclusion, mandatory gates, and weight normalisation.

    score_rows:     RequirementScore ORM rows (final = human_score or raw_score).
    criterion_rows: EvaluationCriterion ORM rows for the RFP.
    Returns the aggregate_vendor_score dict (total_score, section_scores, ...).
    """
    requirement_scores = [
        {
            "criterion_id": str(s.criterion_id) if s.criterion_id else None,
            "raw_score":    float(s.raw_score or 0.0),
            "human_score":  float(s.human_score) if s.human_score is not None else None,
            "compliance_level": getattr(s, "compliance_level", None),
            "category":     "",  # criterion_id is authoritative; label fallback unused
        }
        for s in score_rows
    ]
    criteria = [
        {
            "criterion_id":    str(c.criterion_id) if c.criterion_id else None,
            "parent_id":       str(c.parent_id) if getattr(c, "parent_id", None) else None,
            "label":           c.label or "",
            "section":         c.section or c.label or "",
            "weight":          float(c.weight or 0.0),
            "max_score":       int(c.max_score or 5),
            "criterion_type":  c.criterion_type or "scored",
            "evaluation_mode": getattr(c, "evaluation_mode", None),
        }
        for c in criterion_rows
    ]
    return aggregate_vendor_score(requirement_scores, criteria)


# ── Main evaluation entry point ───────────────────────────────────────────────

async def run_full_evaluation(
    rfp_id: str,
    response_id: str,
    requirements: list[dict],
    criteria: list[dict],
    compliance_levels: Optional[list[dict]] = None,
) -> dict:
    """
    Hybrid matrix-aware evaluation pipeline.

    Step 1  — Concurrent Milvus searches for all requirements.
    Step 2  — Concurrent LLM CoT scoring of requirements.
    Step 2b — Assign criterion_id to each requirement score via label/fuzzy match.
    Step 3  — Route criteria by evaluation_mode:
               • requirement_coverage (or mode=None legacy) criteria whose
                 labels matched in Step 2b are already scored.
               • Unmatched legacy criteria (mode=None) → _score_criterion_directly.
               • All mode-classified criteria that are NOT requirement_coverage
                 → criterion_scorers.score_direct_criteria().
    Step 4  — Weighted aggregation from criterion-level scores.
    """
    logger.info(
        "Evaluation start: rfp=%s response=%s reqs=%d criteria=%d",
        rfp_id, response_id, len(requirements), len(criteria),
    )

    # Step 1 — concurrent Milvus searches
    coverage = await build_coverage_matrix(rfp_id, response_id, requirements)
    logger.info(
        "Coverage: %d/%d covered, %d gaps, %d partial",
        coverage["covered_count"], coverage["total_requirements"],
        coverage["gap_count"], coverage["partial_count"],
    )

    # Step 2 — concurrent LLM CoT scoring
    all_scores = await score_all_requirements(requirements, coverage["entries"], compliance_levels)

    # Step 2b — attach criterion_id to each requirement score (sheet → criterion)
    mapping = map_requirements_to_criteria(requirements, criteria)
    for s in all_scores:
        hit = mapping.get(s.get("req_id"))
        if hit:
            s["criterion_id"], s["mapping_method"] = hit

    logger.info(
        "Scored %d requirements via requirement-coverage path (%d mapped to a criterion)",
        len(all_scores), sum(1 for s in all_scores if s.get("criterion_id")),
    )

    # Step 3a — determine which criteria are handled by direct scorers
    # Criteria whose evaluation_mode is explicitly set and != requirement_coverage
    # are handled by criterion_scorers.  Criteria with mode=None (legacy) that
    # had no requirement scores mapped to them fall through to _score_criterion_directly.

    scored_cids = {s.get("criterion_id") for s in all_scores if s.get("criterion_id")}

    # Criteria to run through the new mode-specific scorers
    # A criterion that has requirements mapped to it is scored FROM those
    # requirements, whatever mode the keyword classifier gave it (e.g.
    # "Architecture & Integration" or "Operations & SLA" were classified as
    # solution_quality and scored holistically, ignoring their 12 requirements).
    leaf_list, parent_list = _select_leaf_criteria(criteria)
    parent_cids = {c.get("criterion_id") for c in parent_list}
    mode_classified_criteria = [
        c for c in leaf_list
        if c.get("criterion_type") not in ("parent",)
        and c.get("criterion_id") not in parent_cids
        and c.get("criterion_id") not in scored_cids
        and c.get("evaluation_mode") not in (None, "requirement_coverage")
    ]

    # Legacy fallback: criteria with evaluation_mode=None not covered by requirements
    legacy_unmatched = [
        c for c in leaf_list
        if c.get("criterion_type") not in ("parent", "matrix")
        and c.get("criterion_id") not in parent_cids
        and c.get("evaluation_mode") is None
        and c.get("criterion_id") not in scored_cids
    ]

    direct_criterion_scores: list[dict] = []

    # Run mode-specific direct scorers
    if mode_classified_criteria:
        logger.info(
            "%d mode-classified criteria → direct scorers",
            len(mode_classified_criteria),
        )
        from app.services.criterion_scorers import score_direct_criteria as _score_direct
        mode_scores = await _score_direct(rfp_id, response_id, mode_classified_criteria)
        direct_criterion_scores.extend(mode_scores)
        logger.info(
            "Direct mode scorers returned %d scores (%d criteria had no evidence)",
            len(mode_scores),
            len(mode_classified_criteria) - len(mode_scores),
        )

    # Run legacy direct scorer for unclassified/unmatched criteria
    if legacy_unmatched:
        logger.info(
            "%d legacy criteria (evaluation_mode=None, unmatched) → legacy scorer",
            len(legacy_unmatched),
        )
        legacy_results = await asyncio.gather(*[
            _score_criterion_directly(rfp_id, response_id, c)
            for c in legacy_unmatched
        ])
        legacy_scores = [r for r in legacy_results if r is not None]
        # Legacy scores are in requirement_score format — add to all_scores
        # (they have req_id=None and criterion_id set, matching existing persistence path)
        all_scores.extend(legacy_scores)
        logger.info(
            "Legacy scorer added %d scores (%d criteria had no evidence)",
            len(legacy_scores),
            len(legacy_unmatched) - len(legacy_scores),
        )

    # Step 4 — weighted aggregation from criterion-level scores
    vendor_score = aggregate_vendor_score(
        requirement_scores=all_scores,
        criteria=criteria,
        direct_criterion_scores=direct_criterion_scores,
    )
    logger.info("Final score: %s", vendor_score["total_score"])

    # Merge direct_criterion_scores into all_scores for persistence
    # (criterion_scorers return dicts with req_id=None and criterion_id set —
    # compatible with persist_vendor_evaluation's RequirementScore insertion)
    all_scores_for_persistence = list(all_scores) + [
        {
            "req_id":           ds.get("req_id"),
            "req_code":         ds.get("req_code", ""),
            "description":      ds.get("description", ""),
            "category":         ds.get("category", ""),
            "criterion_id":     ds.get("criterion_id"),
            "raw_score":        ds.get("raw_score", ds.get("score", 0.0)),
            "compliance_level": ds.get("compliance_level", "NotAddressed"),
            "justification":    ds.get("justification", ""),
            "reasoning":        ds.get("reasoning", ""),
            "citation":         ds.get("citation"),
        }
        for ds in direct_criterion_scores
    ]

    return {
        "coverage":                  coverage,
        "requirement_scores":        all_scores_for_persistence,
        "vendor_score":              vendor_score,
        "direct_criterion_scores":   direct_criterion_scores,
    }


