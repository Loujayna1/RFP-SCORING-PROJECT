"""
Tests for requirement → criterion mapping, verification-first scoring and the
per-vendor score trace (declared vs verified level for every requirement).

Run without the database fixtures:
    pytest --noconftest tests/test_trace_and_mapping.py
"""
from unittest.mock import AsyncMock, patch

import pytest

from app.services.evaluation import (
    aggregate_vendor_score,
    map_requirements_to_criteria,
    resolve_evidence,
    run_full_evaluation,
)
from app.services.trace import build_trace, verification_status

# Grid of the technical RFP dataset: two parents, four leaves (one per sheet)
CRITERIA = [
    {"criterion_id": "p1", "parent_id": None, "label": "1. Solution", "section": "1. Solution", "weight": 0.6},
    {"criterion_id": "c11", "parent_id": "p1", "label": "1.1. Functional Requirements", "section": "", "weight": 0.6},
    {"criterion_id": "c12", "parent_id": "p1", "label": "1.2. Architecture & Integration", "section": "", "weight": 0.4,
     "evaluation_mode": "solution_quality"},
    {"criterion_id": "p2", "parent_id": None, "label": "2. Security & Operations", "section": "", "weight": 0.4},
    {"criterion_id": "c21", "parent_id": "p2", "label": "2.1. Security Requirements", "section": "", "weight": 0.5},
    {"criterion_id": "c22", "parent_id": "p2", "label": "2.2. Operations & SLA", "section": "", "weight": 0.5,
     "evaluation_mode": "solution_quality"},
]
SHEETS = {"Functional Requirements": "c11", "Architecture & Integration": "c12",
          "Security Requirements": "c21", "Operations & SLA": "c22"}


def _reqs():
    out = []
    for sheet, prefix in zip(SHEETS, ("FUN", "ARC", "SEC", "OPS")):
        for i in range(1, 3):
            out.append({"req_id": f"{prefix}-{i}", "req_code": f"{prefix}-{i:02d}",
                        "description": f"{sheet} requirement {i}", "category": sheet})
    return out


# ── Mapping ───────────────────────────────────────────────────────────────────

def test_each_sheet_maps_exactly_to_its_leaf_criterion():
    mapping = map_requirements_to_criteria(_reqs(), CRITERIA)
    for r in _reqs():
        cid, method = mapping[r["req_id"]]
        assert cid == SHEETS[r["category"]]
        assert method == "exact"


def test_parent_criteria_are_never_targets():
    # "Security" alone would have fuzzy-matched the parent "Security & Operations"
    reqs = [{"req_id": "x", "category": "Security"}]
    mapping = map_requirements_to_criteria(reqs, CRITERIA)
    assert mapping["x"][0] == "c21"
    assert mapping["x"][1] == "contains"


def test_unknown_category_is_left_unmapped_not_put_in_other():
    mapping = map_requirements_to_criteria([{"req_id": "y", "category": "Pricing"}], CRITERIA)
    assert "y" not in mapping


# ── Criteria with requirements are scored from them ───────────────────────────

@pytest.mark.asyncio
async def test_criteria_with_requirements_are_not_scored_holistically():
    reqs = _reqs()
    coverage = {"entries": [{"req_id": r["req_id"], "evidence_text": "x", "passages": []} for r in reqs],
                "covered_count": 8, "total_requirements": 8, "gap_count": 0, "partial_count": 0,
                "coverage_rate": 1.0}

    async def fake_score(req, cov, compliance_levels=None):
        return {"req_id": req["req_id"], "req_code": req["req_code"], "description": req["description"],
                "category": req["category"], "raw_score": 5.0, "compliance_level": "FullyCompliant",
                "justification": "", "reasoning": "r", "citation": None, "evidence": None}

    direct = AsyncMock(return_value=[{"criterion_id": "c12", "score": 1.0}, {"criterion_id": "c22", "score": 1.0}])
    with patch("app.services.evaluation.build_coverage_matrix", AsyncMock(return_value=coverage)), \
         patch("app.services.evaluation._score_one_requirement", side_effect=fake_score), \
         patch("app.services.criterion_scorers.score_direct_criteria", direct):
        result = await run_full_evaluation("rfp", "resp", reqs, CRITERIA)

    direct.assert_not_called()                      # Architecture / Operations not sent to the holistic scorer
    assert result["vendor_score"]["total_score"] == 5.0
    assert set(result["vendor_score"]["criterion_scores"]) == {"c11", "c12", "c21", "c22"}


def test_direct_score_does_not_override_requirement_score():
    scores = [{"criterion_id": "c11", "raw_score": 5.0, "compliance_level": "FullyCompliant"}]
    agg = aggregate_vendor_score(scores, CRITERIA, direct_criterion_scores=[{"criterion_id": "c11", "score": 1.0}])
    assert agg["criterion_scores"]["c11"] == 5.0


# ── Evidence resolution ───────────────────────────────────────────────────────

PASSAGES = [
    {"chunk": 1, "text": "General introduction of the company.", "file_name": "p.pdf", "page": 1, "deep_link": "/d?page=1"},
    {"chunk": 2, "text": "Backups are restored with an RPO of 24 hours and an RTO of 48 hours.",
     "file_name": "p.pdf", "page": 7, "deep_link": "/d?page=7"},
]


def test_quote_found_in_a_passage_gives_that_passage_page():
    ev = resolve_evidence({"compliance_level": "PartiallyCompliant", "evidence_chunk": 1,
                           "evidence_quote": "an RPO of 24 hours and an RTO of 48 hours"}, PASSAGES)
    assert ev["page"] == 7 and ev["quote_verified"] is True


def test_chunk_number_used_when_quote_not_found():
    ev = resolve_evidence({"compliance_level": "PartiallyCompliant", "evidence_chunk": 2,
                           "evidence_quote": "paraphrased text"}, PASSAGES)
    assert ev["page"] == 7 and ev["quote_verified"] is False


def test_not_addressed_has_no_evidence():
    assert resolve_evidence({"compliance_level": "NotAddressed", "evidence_chunk": 2}, PASSAGES) is None


# ── Declared vs verified ──────────────────────────────────────────────────────

@pytest.mark.parametrize("declared,verified,expected", [
    ("FullyCompliant", "FullyCompliant", "confirmed"),
    ("FullyCompliant", "PartiallyCompliant", "over_claim"),
    ("FullyCompliant", "NotAddressed", "unsupported"),
    ("PartiallyCompliant", "FullyCompliant", "under_claim"),
    (None, "FullyCompliant", "not_declared"),
    ("FullyCompliant", "ScoringFailed", "scoring_failed"),
])
def test_verification_status(declared, verified, expected):
    assert verification_status(declared, verified) == expected


def test_trace_criterion_scores_add_up_to_total_and_show_declared_total():
    levels = {"FUN-1": "FullyCompliant", "FUN-2": "PartiallyCompliant", "ARC-1": "FullyCompliant",
              "ARC-2": "FullyCompliant", "SEC-1": "NotAddressed", "SEC-2": "FullyCompliant",
              "OPS-1": "RoadmapCompliant", "OPS-2": "NonCompliant"}
    pts = {"FullyCompliant": 5, "PartiallyCompliant": 3, "RoadmapCompliant": 2, "NonCompliant": 1, "NotAddressed": 0}
    scores = []
    for r in _reqs():
        lvl = levels[r["req_id"]]
        scores.append({**r, "criterion_id": SHEETS[r["category"]], "compliance_level": lvl,
                       "raw_score": pts[lvl], "final_score": pts[lvl], "human_score": None,
                       "evidence_page": 3})
    declarations = {r["req_code"]: {"declared_level": "FullyCompliant", "reference": "Proposal, §5, p. 3"}
                    for r in _reqs()}
    scale = [{"label": "Fully Compliant", "value": 5}, {"label": "Partially Compliant", "value": 3},
             {"label": "Roadmap Compliant", "value": 2}, {"label": "Non-Compliant", "value": 1}]

    t = build_trace(criteria=CRITERIA, scores=scores, declarations=declarations, compliance_levels=scale)

    leaves = [ch for root in t["criteria"] for ch in root["children"]]
    assert [l["score"] for l in leaves] == [4.0, 5.0, 2.5, 1.5]
    # contributions of the leaves sum to the total
    assert abs(sum(l["contribution"] for l in leaves) - t["total_score"]) < 1e-3
    # 0.36*4 + 0.24*5 + 0.2*2.5 + 0.2*1.5 = 3.44
    assert t["total_score"] == pytest.approx(3.44, abs=1e-3)
    assert t["declared_total"] == 5.0
    # parents are weighted subtotals, not 0 / NaN
    assert t["criteria"][0]["score"] == pytest.approx(4.4, abs=1e-3)
    v = t["summary"]["verification"]
    assert v == {"confirmed": 4, "over_claim": 3, "unsupported": 1}
    assert t["summary"]["references_matching"] == 8
