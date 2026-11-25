"""
Score trace for one vendor: how every requirement was scored and how the
requirement scores add up to the criterion scores and to the total.

For each requirement it puts side by side what the vendor DECLARED in its
compliance matrix and what the system VERIFIED in its proposal, with the
evidence sentence, document and page, and classifies the outcome:

  confirmed      the proposal supports the declared level
  over_claim     the proposal supports a lower level than declared
  unsupported    a level was declared but nothing in the proposal addresses it
  under_claim    the proposal supports a higher level than declared
  not_declared   the vendor's matrix gives no level for this requirement
  scoring_failed the automated assessment failed; human review needed

Criterion scores come from the same aggregation as the vendor's total
(aggregate_vendor_score), so the figures shown always add up to the total.
"""
from __future__ import annotations

import re
from typing import Optional

from app.services.evaluation import (
    _criterion_code,
    _select_leaf_criteria,
    aggregate_vendor_score,
    compute_effective_weights,
)
from app.services.vendor_matrix import reference_page

LEVEL_RANK = {
    "FullyCompliant": 4,
    "PartiallyCompliant": 3,
    "RoadmapCompliant": 2,
    "NonCompliant": 1,
    "NotAddressed": 0,
}

_LEVEL_KEYS = {
    "fullycompliant": "FullyCompliant",
    "partiallycompliant": "PartiallyCompliant",
    "roadmapcompliant": "RoadmapCompliant",
    "noncompliant": "NonCompliant",
    "notcompliant": "NonCompliant",
    "notaddressed": "NotAddressed",
}


def _key(text) -> str:
    return re.sub(r"[^a-z]", "", str(text or "").lower())


def level_points(compliance_levels: Optional[list[dict]]) -> dict[str, float]:
    """Points of each canonical level on the RFP's configured scale."""
    points = {"NotAddressed": 0.0}
    for lvl in compliance_levels or []:
        name = _LEVEL_KEYS.get(_key(lvl.get("label")))
        if name:
            try:
                points[name] = float(lvl.get("value", 0))
            except (TypeError, ValueError):
                pass
    return points


def verification_status(declared: Optional[str], verified: Optional[str]) -> str:
    if verified == "ScoringFailed":
        return "scoring_failed"
    if not declared:
        return "not_declared"
    d, v = LEVEL_RANK.get(declared), LEVEL_RANK.get(verified or "NotAddressed", 0)
    if d is None:
        return "not_declared"
    if v == d:
        return "confirmed"
    if v < d:
        return "unsupported" if verified == "NotAddressed" else "over_claim"
    return "under_claim"


def _sort_key(code: Optional[str]):
    parts = re.findall(r"\d+", code or "")
    return [int(p) for p in parts] or [10 ** 6]


def build_trace(
    *,
    criteria: list[dict],
    scores: list[dict],
    declarations: dict[str, dict],
    compliance_levels: Optional[list[dict]] = None,
) -> dict:
    """
    criteria:     criterion dicts (criterion_id, parent_id, label, section,
                  weight, criterion_type, evaluation_mode)
    scores:       requirement score dicts (req_id, req_code, description,
                  category, criterion_id, compliance_level, raw_score,
                  human_score, final_score, evidence fields, ...)
    declarations: {req_code: {declared_level, declared_raw, response_text, reference}}
    """
    points = level_points(compliance_levels)
    effective = compute_effective_weights(criteria)
    leaves, parents = _select_leaf_criteria(criteria)
    leaf_ids = {c.get("criterion_id") for c in leaves}

    # Same aggregation as the vendor's total, on verified and on declared levels.
    verified_agg = aggregate_vendor_score(scores, criteria)
    declared_rows = []
    for s in scores:
        d = declarations.get(s.get("req_code") or "")
        if d and d.get("declared_level") in points:
            declared_rows.append({**s, "raw_score": points[d["declared_level"]],
                                  "human_score": None, "compliance_level": d["declared_level"]})
    declared_agg = aggregate_vendor_score(declared_rows, criteria) if declared_rows else None
    weight_sum = verified_agg.get("weight_sum") or 0.0

    # Requirements with their declared vs verified levels
    rows_by_cid: dict[Optional[str], list[dict]] = {}
    status_counts: dict[str, int] = {}
    for s in scores:
        if not s.get("req_id"):
            continue                    # criterion-level direct scores are not requirements
        d = declarations.get(s.get("req_code") or "") or {}
        declared = d.get("declared_level")
        verified = s.get("compliance_level")
        status = verification_status(declared, verified)
        status_counts[status] = status_counts.get(status, 0) + 1
        ref_page = reference_page(d.get("reference"))
        ev_page = s.get("evidence_page")
        row = {
            **s,
            "declared_level":   declared,
            "declared_raw":     d.get("declared_raw"),
            "declared_points":  points.get(declared) if declared else None,
            "vendor_response":  d.get("response_text"),
            "vendor_reference": d.get("reference"),
            "reference_page":   ref_page,
            "reference_matches_evidence": (ref_page == ev_page) if (ref_page and ev_page) else None,
            "verification":     status,
        }
        cid = s.get("criterion_id") if s.get("criterion_id") in leaf_ids else None
        rows_by_cid.setdefault(cid, []).append(row)

    for rows in rows_by_cid.values():
        rows.sort(key=lambda r: (r.get("matrix_sheet_order") or 0, r.get("matrix_row_order") or 0,
                                 r.get("req_code") or ""))

    crit_scores = verified_agg.get("criterion_scores", {})
    decl_scores = (declared_agg or {}).get("criterion_scores", {})

    def _level_counts(rows):
        counts: dict[str, int] = {}
        for r in rows:
            counts[r.get("compliance_level") or "NotAddressed"] = counts.get(r.get("compliance_level") or "NotAddressed", 0) + 1
        return counts

    def node(c: dict) -> dict:
        cid = c.get("criterion_id")
        eff = effective.get(cid, 0.0)
        score = crit_scores.get(cid)
        rows = rows_by_cid.get(cid, []) if cid in leaf_ids else []
        counted = [r for r in rows if not (r.get("human_score") is None and r.get("compliance_level") == "ScoringFailed")]
        out = {
            "criterion_id":     cid,
            "code":             _criterion_code(c),
            "label":            c.get("label"),
            "is_leaf":          cid in leaf_ids,
            "local_weight":     c.get("weight"),
            "effective_weight": round(eff, 6),
            "evaluation_mode":  c.get("evaluation_mode"),
            "score":            score,
            "declared_score":   decl_scores.get(cid),
            "contribution":     round(score * eff / weight_sum, 4) if (score is not None and weight_sum) else None,
            "scored_from":      ("requirements" if rows else ("direct" if score is not None else "not_scored")),
            "requirement_count": len(rows),
            "requirements_counted": len(counted),
            "points_sum":       round(sum(float(r.get("final_score") or 0.0) for r in counted), 3),
            "level_counts":     _level_counts(rows),
            "requirements":     rows,
            "children":         [],
        }
        return out

    nodes = {c.get("criterion_id"): node(c) for c in criteria if c.get("criterion_id")}
    roots = []
    for c in criteria:
        cid = c.get("criterion_id")
        pid = c.get("parent_id")
        if pid and pid in nodes:
            nodes[pid]["children"].append(nodes[cid])
        elif cid in nodes:
            roots.append(nodes[cid])

    def finish(n: dict) -> None:
        n["children"].sort(key=lambda x: _sort_key(x["code"]))
        for ch in n["children"]:
            finish(ch)
        if not n["is_leaf"]:
            # Parent: weighted average of its scored children (SUMPRODUCT of
            # the evaluation matrix), shown for reading only.
            scored = [ch for ch in n["children"] if ch["score"] is not None and ch["effective_weight"] > 0]
            w = sum(ch["effective_weight"] for ch in scored)
            n["score"] = round(sum(ch["score"] * ch["effective_weight"] for ch in scored) / w, 3) if w else None
            dsc = [ch for ch in n["children"] if ch["declared_score"] is not None and ch["effective_weight"] > 0]
            dw = sum(ch["effective_weight"] for ch in dsc)
            n["declared_score"] = round(sum(ch["declared_score"] * ch["effective_weight"] for ch in dsc) / dw, 3) if dw else None
            n["contribution"] = round(sum(ch["contribution"] or 0 for ch in n["children"]), 4)
            n["scored_from"] = "children"
            n["requirement_count"] = sum(ch["requirement_count"] for ch in n["children"])

    roots.sort(key=lambda x: _sort_key(x["code"]))
    for r in roots:
        finish(r)

    unmapped = rows_by_cid.get(None, [])
    total_reqs = sum(len(v) for v in rows_by_cid.values())
    evidence_found = sum(
        1 for v in rows_by_cid.values() for r in v
        if r.get("compliance_level") not in (None, "NotAddressed", "ScoringFailed")
    )
    refs = [r for v in rows_by_cid.values() for r in v if r.get("reference_matches_evidence") is not None]

    return {
        "total_score":        verified_agg.get("total_score"),
        "declared_total":     (declared_agg or {}).get("total_score"),
        "weight_sum":         weight_sum,
        "criteria":           roots,
        "unmapped_requirements": unmapped,
        "unscored_criteria":  verified_agg.get("unscored_criteria", []),
        "level_points":       points,
        "summary": {
            "requirements":      total_reqs,
            "evidence_found":    evidence_found,
            "evidence_rate":     round(evidence_found / total_reqs, 4) if total_reqs else 0.0,
            "verification":      status_counts,
            "references_checked": len(refs),
            "references_matching": sum(1 for r in refs if r["reference_matches_evidence"]),
        },
    }
