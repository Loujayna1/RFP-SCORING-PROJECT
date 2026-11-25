"""
Unit tests for aggregate_vendor_score — bugs #2 (parent/child double-count)
and #5 (no-evidence auto-zero).

aggregate_vendor_score is pure and synchronous; no DB or Milvus needed.
"""

import types

from app.services.evaluation import (
    aggregate_vendor_score,
    _select_leaf_criteria,
    recompute_total_from_rows,
)


# Mirrors the real "cloud" RFP: a flat extraction path saved every row —
# parents included — as criterion_type="scored", with the hierarchy surviving
# only in the leading codes of the labels.
CLOUD_CRITERIA = [
    {"criterion_id": "c1",   "label": "1.Proposed Services",      "weight": 0.8, "criterion_type": "scored"},
    {"criterion_id": "c12",  "label": "1.2.Submission Quality",   "weight": 0.2, "criterion_type": "scored"},
    {"criterion_id": "c13",  "label": "1.3. Functional",          "weight": 0.3, "criterion_type": "scored"},
    {"criterion_id": "c15",  "label": "1.5. Pricing evaluation",  "weight": 0.3, "criterion_type": "scored"},
    {"criterion_id": "c2",   "label": "2.References",             "weight": 0.2, "criterion_type": "scored"},
    {"criterion_id": "c21",  "label": "2.1.Portfolio cloud",      "weight": 0.1, "criterion_type": "scored"},
]


def test_structural_leaf_detection_excludes_code_parents():
    leaves, parents = _select_leaf_criteria(CLOUD_CRITERIA)
    parent_labels = {c["label"] for c in parents}
    assert parent_labels == {"1.Proposed Services", "2.References"}
    assert {c["criterion_id"] for c in leaves} == {"c12", "c13", "c15", "c21"}


def test_parent_id_graph_detection():
    criteria = [
        {"criterion_id": "p", "label": "Technical", "weight": 1.0, "parent_id": None},
        {"criterion_id": "a", "label": "Perf",      "weight": 0.5, "parent_id": "p"},
        {"criterion_id": "b", "label": "Security",  "weight": 0.5, "parent_id": "p"},
    ]
    leaves, parents = _select_leaf_criteria(criteria)
    assert [c["criterion_id"] for c in parents] == ["p"]
    assert {c["criterion_id"] for c in leaves} == {"a", "b"}


def test_parent_not_double_counted_in_total():
    """A parent scoring high must not inflate the total past its children."""
    req_scores = [
        {"criterion_id": "c13", "raw_score": 3.0, "category": "1.3. Functional"},
        {"criterion_id": "c15", "raw_score": 0.0, "category": "1.5. Pricing evaluation"},
        {"criterion_id": "c21", "raw_score": 0.0, "category": "2.1.Portfolio cloud"},
    ]
    # Direct score makes the PARENT "1.Proposed Services" look like a 5.0.
    direct = [{"criterion_id": "c1", "score": 5.0}]

    result = aggregate_vendor_score(req_scores, CLOUD_CRITERIA, direct)

    # Weighted total over leaves only (c12 has no evidence → excluded):
    #   (3.0*0.3 + 0.0*0.3 + 0.0*0.1) / (0.3+0.3+0.1) = 0.9/0.7 ≈ 1.286
    # The parent's 5.0 and weight 0.8 must NOT appear.
    assert result["total_score"] == 1.286
    assert "1.Proposed Services" not in _weighted_labels(result)


def test_no_evidence_criterion_excluded_not_zeroed():
    """A leaf with no mapped evidence is surfaced, not charged as a hard 0."""
    req_scores = [
        {"criterion_id": "c13", "raw_score": 4.0, "category": "1.3. Functional"},
    ]
    result = aggregate_vendor_score(req_scores, CLOUD_CRITERIA, [])

    unscored_labels = {u["label"] for u in result["unscored_criteria"]}
    # Every leaf except the one scored (c13) had no evidence.
    assert "1.2.Submission Quality" in unscored_labels
    assert "1.5. Pricing evaluation" in unscored_labels
    assert "2.1.Portfolio cloud" in unscored_labels

    # Total is the single scored leaf, NOT dragged down by the empty ones.
    assert result["total_score"] == 4.0


def test_genuine_zero_still_counts():
    """A real NonCompliant 0 (has evidence) must still lower the total."""
    req_scores = [
        {"criterion_id": "c13", "raw_score": 4.0, "category": "1.3. Functional"},
        {"criterion_id": "c15", "raw_score": 0.0, "category": "1.5. Pricing evaluation"},
    ]
    result = aggregate_vendor_score(req_scores, CLOUD_CRITERIA, [])
    # (4.0*0.3 + 0.0*0.3) / (0.3+0.3) = 1.2/0.6 = 2.0
    assert result["total_score"] == 2.0


def _weighted_labels(result):
    """Labels that ended up in section_scores (scored criteria)."""
    return set(result["section_scores"].keys())


# ── Override/consensus paths now share the pipeline's aggregation ─────────────

def _crit_row(cid, label, weight, ctype="scored", parent_id=None):
    return types.SimpleNamespace(
        criterion_id=cid, parent_id=parent_id, label=label, section=label,
        weight=weight, max_score=5, criterion_type=ctype, evaluation_mode=None,
    )


def _score_row(cid, raw, human=None):
    return types.SimpleNamespace(criterion_id=cid, raw_score=raw, human_score=human)


def test_recompute_from_rows_matches_pipeline():
    """The override/consensus recompute must equal aggregate_vendor_score for
    the same inputs — no second, divergent formula."""
    criterion_rows = [
        _crit_row("c1",  "1.Proposed Services",  0.8),  # parent (has children)
        _crit_row("c13", "1.3. Functional",      0.3, parent_id="c1"),
        _crit_row("c15", "1.5. Pricing",         0.3, parent_id="c1"),
        _crit_row("c2",  "2.References",          0.2),  # parent
        _crit_row("c21", "2.1.Portfolio",         0.1, parent_id="c2"),
    ]
    score_rows = [
        _score_row("c13", 3.0),
        _score_row("c15", 0.0, human=4.0),   # a human override in play
        _score_row("c21", 2.0),
    ]

    via_rows = recompute_total_from_rows(score_rows, criterion_rows)

    # Equivalent dict inputs fed straight to the pipeline aggregator.
    reqs = [
        {"criterion_id": "c13", "raw_score": 3.0, "human_score": None, "category": ""},
        {"criterion_id": "c15", "raw_score": 0.0, "human_score": 4.0,  "category": ""},
        {"criterion_id": "c21", "raw_score": 2.0, "human_score": None, "category": ""},
    ]
    crits = [
        {"criterion_id": "c1",  "parent_id": None, "label": "1.Proposed Services", "weight": 0.8, "criterion_type": "scored"},
        {"criterion_id": "c13", "parent_id": "c1", "label": "1.3. Functional",     "weight": 0.3, "criterion_type": "scored"},
        {"criterion_id": "c15", "parent_id": "c1", "label": "1.5. Pricing",        "weight": 0.3, "criterion_type": "scored"},
        {"criterion_id": "c2",  "parent_id": None, "label": "2.References",        "weight": 0.2, "criterion_type": "scored"},
        {"criterion_id": "c21", "parent_id": "c2", "label": "2.1.Portfolio",       "weight": 0.1, "criterion_type": "scored"},
    ]
    via_pipeline = aggregate_vendor_score(reqs, crits)

    assert via_rows["total_score"] == via_pipeline["total_score"]
    # human_score wins over raw_score, parents excluded, and each leaf carries
    # its EFFECTIVE weight (its share of the parent's weight), as in the
    # SUMPRODUCT formulas of the company's evaluation spreadsheets:
    #   c13 = 0.8 * 0.3/0.6 = 0.4, c15 = 0.4, c21 = 0.2 * 0.1/0.1 = 0.2
    #   (3.0*0.4 + 4.0*0.4 + 2.0*0.2) / 1.0 = 3.2
    assert via_rows["total_score"] == 3.2


def test_mandatory_criterion_excluded_from_total():
    criteria = [
        {"criterion_id": "a", "label": "Weighted", "weight": 1.0, "criterion_type": "scored"},
        {"criterion_id": "m", "label": "Gate",     "weight": 1.0, "criterion_type": "mandatory"},
    ]
    reqs = [
        {"criterion_id": "a", "raw_score": 4.0, "category": ""},
        {"criterion_id": "m", "raw_score": 0.0, "category": ""},  # mandatory 0 must NOT drag total
    ]
    result = aggregate_vendor_score(reqs, criteria)
    assert result["total_score"] == 4.0


# ── Effective weights ─────────────────────────────────────────────────────────

def test_effective_weights_relative_subweights():
    """Sub-weights summing to 1 within each parent are multiplied by the parent."""
    from app.services.evaluation import compute_effective_weights
    crits = [
        {"criterion_id": "p1", "parent_id": None, "weight": 0.7},
        {"criterion_id": "a",  "parent_id": "p1", "weight": 0.35},
        {"criterion_id": "b",  "parent_id": "p1", "weight": 0.65},
        {"criterion_id": "p2", "parent_id": None, "weight": 0.2},
        {"criterion_id": "c",  "parent_id": "p2", "weight": 1.0},
        {"criterion_id": "p3", "parent_id": None, "weight": 0.1},
        {"criterion_id": "d",  "parent_id": "p3", "weight": 0.4},
        {"criterion_id": "e",  "parent_id": "p3", "weight": 0.6},
    ]
    eff = compute_effective_weights(crits)
    assert round(eff["a"], 4) == 0.245
    assert round(eff["c"], 4) == 0.2
    assert round(eff["d"], 4) == 0.04
    assert round(sum(eff[k] for k in "abcde"), 6) == 1.0


def test_effective_weights_absolute_subweights():
    """Sub-weights summing to the parent's weight give the same shares."""
    from app.services.evaluation import compute_effective_weights
    crits = [
        {"criterion_id": "p1", "parent_id": None, "weight": 40},
        {"criterion_id": "a",  "parent_id": "p1", "weight": 25},
        {"criterion_id": "b",  "parent_id": "p1", "weight": 15},
        {"criterion_id": "p2", "parent_id": None, "weight": 60},
        {"criterion_id": "c",  "parent_id": "p2", "weight": 60},
    ]
    eff = compute_effective_weights(crits)
    assert round(eff["a"], 4) == 0.25
    assert round(eff["b"], 4) == 0.15
    assert round(eff["c"], 4) == 0.6


def test_main_criteria_keep_their_influence():
    """With relative sub-weights, a strong score on a 0.7 criterion must weigh
    more than the same score on a 0.1 criterion (leaf weights alone made all
    main criteria count equally)."""
    crits = [
        {"criterion_id": "p1", "parent_id": None, "label": "1. Services",  "weight": 0.7, "criterion_type": "scored"},
        {"criterion_id": "a",  "parent_id": "p1", "label": "1.1. Func",    "weight": 1.0, "criterion_type": "scored"},
        {"criterion_id": "p2", "parent_id": None, "label": "2. Resources", "weight": 0.3, "criterion_type": "scored"},
        {"criterion_id": "b",  "parent_id": "p2", "label": "2.1. Team",    "weight": 1.0, "criterion_type": "scored"},
    ]
    reqs = [
        {"criterion_id": "a", "raw_score": 5.0, "human_score": None, "category": ""},
        {"criterion_id": "b", "raw_score": 1.0, "human_score": None, "category": ""},
    ]
    assert aggregate_vendor_score(reqs, crits)["total_score"] == 3.8   # 5*0.7 + 1*0.3

