"""
Tests for the hybrid matrix-aware evaluation layer.

Coverage
--------
1. parse_evaluation_matrix_xlsx — basic hierarchical matrix
2. parse_evaluation_matrix_xlsx — matrix with % weights
3. parse_evaluation_matrix_xlsx — fails loudly when structure is invalid
4. classify_evaluation_mode — correct mode per label
5. aggregate_vendor_score — requirement-coverage criteria only (backward compat)
6. aggregate_vendor_score — mixed requirement + direct criterion scores
7. aggregate_vendor_score — direct-only criteria (no requirements)
8. score_direct_criteria dispatch — mode routing (mocked scorers)
"""

import io
import asyncio
import pytest
import openpyxl

from app.services.criterion_classifier import classify_evaluation_mode, MODE_RULES
from app.services.evaluation_matrix_parser import (
    parse_evaluation_matrix_xlsx,
    flat_criteria_from_tree,
    EvaluationMatrixParseError,
)
from app.services.evaluation import aggregate_vendor_score


# ─────────────────────────────────────────────────────────────────────────────
# Helpers — build in-memory XLSX workbooks
# ─────────────────────────────────────────────────────────────────────────────

def _make_xlsx(rows: list[tuple], sheet_name: str = "Evaluation Criteria") -> bytes:
    """Build a minimal XLSX with a single sheet from a list of row tuples."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_name
    for row in rows:
        ws.append(list(row))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _standard_matrix_bytes() -> bytes:
    """
    A typical 2-level evaluation matrix:
      Code | Criterion                         | Weight
      1    | Proposed Services                 | 40
      1.1  | Functional requirements           | 25
      1.2  | Technical requirements            | 15
      2    | Company Profile                   | 20
      2.1  | Portfolio of similar projects     | 10
      2.2  | Experience of proposed resources  | 10
      3    | Proposed Solution                 | 30
      3.1  | Project implementation plan       | 15
      3.2  | Quality of proposal               | 15
      4    | Financial Offer                   | 10
      4.1  | Pricing                           | 10
    """
    rows = [
        ("Code", "Criterion",                          "Weight"),
        ("1",    "Proposed Services",                  40),
        ("1.1",  "Functional requirements",            25),
        ("1.2",  "Technical requirements",             15),
        ("2",    "Company Profile",                    20),
        ("2.1",  "Portfolio of similar projects",      10),
        ("2.2",  "Experience of proposed resources",   10),
        ("3",    "Proposed Solution",                  30),
        ("3.1",  "Project implementation plan",        15),
        ("3.2",  "Quality of proposal",                15),
        ("4",    "Financial Offer",                    10),
        ("4.1",  "Pricing",                            10),
    ]
    return _make_xlsx(rows)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Parse a valid hierarchical matrix
# ─────────────────────────────────────────────────────────────────────────────

def test_parse_basic_matrix():
    tree = parse_evaluation_matrix_xlsx(_standard_matrix_bytes(), "evaluation_matrix.xlsx")
    flat = flat_criteria_from_tree(tree)

    codes = [c["_code"] for c in flat]
    assert "1"   in codes, "Parent code '1' missing"
    assert "1.1" in codes, "Leaf code '1.1' missing"
    assert "4.1" in codes, "Leaf code '4.1' missing"

    # Count leaves vs parents
    leaves  = [c for c in flat if c["criterion_type"] == "leaf"]
    parents = [c for c in flat if c["criterion_type"] == "parent"]
    assert len(leaves)  == 7, f"Expected 7 leaf criteria, got {len(leaves)}"
    assert len(parents) == 4, f"Expected 4 parent criteria, got {len(parents)}"


def test_parse_matrix_evaluation_mode_assigned_to_leaves_only():
    tree = parse_evaluation_matrix_xlsx(_standard_matrix_bytes(), "evaluation_matrix.xlsx")
    flat = flat_criteria_from_tree(tree)

    for c in flat:
        if c["criterion_type"] == "leaf":
            assert c["evaluation_mode"] is not None, f"Leaf '{c['label']}' has no evaluation_mode"
        else:
            assert c["evaluation_mode"] is None, f"Parent '{c['label']}' should not have evaluation_mode"


def test_parse_matrix_weight_extracted():
    tree = parse_evaluation_matrix_xlsx(_standard_matrix_bytes(), "evaluation_matrix.xlsx")
    flat = flat_criteria_from_tree(tree)

    leaf_11 = next(c for c in flat if c["_code"] == "1.1")
    assert leaf_11["weight"] == 25.0, f"Expected weight=25.0, got {leaf_11['weight']}"


def test_parse_matrix_percent_weights():
    """Weights given as '35%' strings should be parsed to 35.0."""
    rows = [
        ("Code", "Criterion",               "Weight"),
        ("1",    "Proposed Services",       "40%"),
        ("1.1",  "Functional requirements", "25%"),
        ("1.2",  "Technical requirements",  "15%"),
    ]
    tree = parse_evaluation_matrix_xlsx(_make_xlsx(rows), "evaluation_matrix.xlsx")
    flat = flat_criteria_from_tree(tree)
    leaf = next(c for c in flat if c["_code"] == "1.1")
    assert leaf["weight"] == 25.0


def test_parse_matrix_tree_hierarchy():
    """Children of '1' must be '1.1' and '1.2'."""
    tree = parse_evaluation_matrix_xlsx(_standard_matrix_bytes(), "evaluation_matrix.xlsx")
    # First root is code '1'
    root1 = next(n for n in tree if n["_code"] == "1")
    child_codes = {c["_code"] for c in root1["children"]}
    assert child_codes == {"1.1", "1.2"}


# ─────────────────────────────────────────────────────────────────────────────
# 2. Parser fail-loudly on invalid matrix
# ─────────────────────────────────────────────────────────────────────────────

def test_parse_matrix_fails_on_empty_sheet():
    """Workbook with no hierarchical codes must raise EvaluationMatrixParseError."""
    rows = [
        ("Item",   "Description",          "Comment"),
        ("Note 1", "This is a note",       ""),
        ("Note 2", "Another note",         ""),
    ]
    with pytest.raises(EvaluationMatrixParseError):
        parse_evaluation_matrix_xlsx(_make_xlsx(rows, "Evaluation"), "evaluation.xlsx")


def test_parse_matrix_fails_on_no_label_column():
    """Sheet with only numeric codes and no description column."""
    rows = [
        ("1",   ""),
        ("1.1", ""),
        ("1.2", ""),
    ]
    with pytest.raises(EvaluationMatrixParseError):
        parse_evaluation_matrix_xlsx(_make_xlsx(rows, "Evaluation"), "evaluation.xlsx")


def test_parse_matrix_fails_on_corrupt_file():
    with pytest.raises(EvaluationMatrixParseError):
        parse_evaluation_matrix_xlsx(b"not an xlsx file at all", "evaluation.xlsx")


# ─────────────────────────────────────────────────────────────────────────────
# 3. classify_evaluation_mode
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("label,parent,expected_mode", [
    # requirement_coverage
    ("Functional requirements",           "Proposed Services",    "requirement_coverage"),
    ("Technical requirements",            "Proposed Services",    "requirement_coverage"),
    ("Security requirements",             None,                   "requirement_coverage"),
    ("Operational requirements",          None,                   "requirement_coverage"),
    ("Infrastructure requirements",       None,                   "requirement_coverage"),
    ("Integration requirements",          "Technical",            "requirement_coverage"),
    # plan_methodology
    ("Project implementation plan",       "Proposed Solution",    "plan_methodology"),
    ("Implementation methodology",        None,                   "plan_methodology"),
    ("Roadmap and timeline",              None,                   "plan_methodology"),
    ("Milestones and deliverables",       None,                   "plan_methodology"),
    ("Project governance model",          None,                   "plan_methodology"),
    # experience_references
    ("Portfolio of similar projects",     "Company Profile",      "experience_references"),
    ("References and case studies",       None,                   "experience_references"),
    ("Prior experience in telecom",       None,                   "experience_references"),
    ("Comparable implementations",        None,                   "experience_references"),
    # team_capability
    ("Experience of proposed resources",  "Company Profile",      "team_capability"),
    ("Team composition and certifications", None,                 "team_capability"),
    ("CVs and qualifications",            None,                   "team_capability"),
    ("Number of assigned consultants",    None,                   "team_capability"),
    # solution_quality
    ("Quality of the proposal",           None,                   "solution_quality"),
    ("Clarity of the response",           None,                   "solution_quality"),
    ("Overall quality of the submission", None,                   "solution_quality"),
    # commercial
    ("Pricing",                           "Financial Offer",      "commercial"),
    ("Financial proposal",                None,                   "commercial"),
    ("Commercial terms",                  None,                   "commercial"),
    ("Total cost of ownership",           None,                   "commercial"),
])
def test_classify_evaluation_mode(label, parent, expected_mode):
    result = classify_evaluation_mode(label, parent)
    assert result == expected_mode, (
        f"classify_evaluation_mode({label!r}, {parent!r}) = {result!r}, expected {expected_mode!r}"
    )


def test_classify_unknown_defaults_to_solution_quality():
    result = classify_evaluation_mode("Miscellaneous criterion xyz", None)
    assert result == "solution_quality"


# ─────────────────────────────────────────────────────────────────────────────
# 4. aggregate_vendor_score — requirement coverage only (backward compat)
# ─────────────────────────────────────────────────────────────────────────────

def _make_criteria(label_weight_mode: list[tuple]) -> list[dict]:
    import uuid
    return [
        {
            "criterion_id":   str(uuid.uuid4()),
            "label":          lbl,
            "section":        lbl,
            "weight":         w,
            "max_score":      5,
            "criterion_type": "leaf",
            "evaluation_mode": mode,
        }
        for lbl, w, mode in label_weight_mode
    ]


def test_aggregate_requirement_coverage_only():
    criteria = _make_criteria([
        ("Functional", 35.0, "requirement_coverage"),
        ("Technical",  35.0, "requirement_coverage"),
        ("Security",   30.0, "requirement_coverage"),
    ])
    req_scores = [
        {"req_id": "r1", "category": "Functional", "raw_score": 4.0, "criterion_id": criteria[0]["criterion_id"]},
        {"req_id": "r2", "category": "Functional", "raw_score": 3.0, "criterion_id": criteria[0]["criterion_id"]},
        {"req_id": "r3", "category": "Technical",  "raw_score": 5.0, "criterion_id": criteria[1]["criterion_id"]},
        {"req_id": "r4", "category": "Security",   "raw_score": 2.0, "criterion_id": criteria[2]["criterion_id"]},
    ]

    result = aggregate_vendor_score(req_scores, criteria)

    assert "total_score"   in result
    assert "section_scores" in result
    # Functional mean = (4+3)/2 = 3.5; Technical mean = 5; Security mean = 2
    # Weighted = (3.5*35 + 5*35 + 2*30) / 100 = (122.5 + 175 + 60) / 100 = 3.575
    assert abs(result["total_score"] - 3.575) < 0.01, f"Expected ~3.575, got {result['total_score']}"


def test_aggregate_zero_weight_guard():
    """All weights=0 → fall back to mean of section_scores."""
    criteria = _make_criteria([
        ("A", 0.0, "requirement_coverage"),
        ("B", 0.0, "requirement_coverage"),
    ])
    req_scores = [
        {"req_id": "r1", "category": "A", "raw_score": 4.0, "criterion_id": criteria[0]["criterion_id"]},
        {"req_id": "r2", "category": "B", "raw_score": 2.0, "criterion_id": criteria[1]["criterion_id"]},
    ]
    result = aggregate_vendor_score(req_scores, criteria)
    assert result["total_score"] == 3.0, f"Expected 3.0 (mean), got {result['total_score']}"


# ─────────────────────────────────────────────────────────────────────────────
# 5. aggregate_vendor_score — mixed requirement + direct criterion scores
# ─────────────────────────────────────────────────────────────────────────────

def test_aggregate_mixed_requirement_and_direct():
    criteria = _make_criteria([
        ("Functional requirements",           25.0, "requirement_coverage"),
        ("Portfolio of similar projects",     15.0, "experience_references"),
        ("Project implementation plan",       10.0, "plan_methodology"),
    ])
    req_cid = criteria[0]["criterion_id"]

    req_scores = [
        {"req_id": "r1", "category": "Functional requirements", "raw_score": 4.0, "criterion_id": req_cid},
        {"req_id": "r2", "category": "Functional requirements", "raw_score": 3.0, "criterion_id": req_cid},
    ]
    direct_scores = [
        {
            "criterion_id":     criteria[1]["criterion_id"],
            "score":            3.5,
            "evaluation_mode":  "experience_references",
        },
        {
            "criterion_id":     criteria[2]["criterion_id"],
            "score":            4.0,
            "evaluation_mode":  "plan_methodology",
        },
    ]

    result = aggregate_vendor_score(req_scores, criteria, direct_criterion_scores=direct_scores)

    # Functional mean = (4+3)/2 = 3.5 (weight 25)
    # References = 3.5 (weight 15)
    # Plan = 4.0 (weight 10)
    # Total = (3.5*25 + 3.5*15 + 4.0*10) / 50 = (87.5 + 52.5 + 40) / 50 = 3.6
    assert abs(result["total_score"] - 3.6) < 0.01, f"Expected ~3.6, got {result['total_score']}"
    assert "Portfolio of similar projects" in result["section_scores"]
    assert "Project implementation plan"  in result["section_scores"]


def test_aggregate_direct_only_no_requirements():
    """Purely direct criteria (no requirement scoring at all)."""
    criteria = _make_criteria([
        ("Portfolio of similar projects",     50.0, "experience_references"),
        ("Project implementation plan",       50.0, "plan_methodology"),
    ])
    direct_scores = [
        {"criterion_id": criteria[0]["criterion_id"], "score": 4.0, "evaluation_mode": "experience_references"},
        {"criterion_id": criteria[1]["criterion_id"], "score": 2.0, "evaluation_mode": "plan_methodology"},
    ]
    result = aggregate_vendor_score([], criteria, direct_criterion_scores=direct_scores)
    # (4*50 + 2*50) / 100 = 3.0
    assert abs(result["total_score"] - 3.0) < 0.01, f"Expected 3.0, got {result['total_score']}"


# ─────────────────────────────────────────────────────────────────────────────
# 6. score_direct_criteria dispatch (mocked)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_score_direct_criteria_dispatch():
    """
    score_direct_criteria should dispatch each criterion to the right scorer.
    We patch SCORER_BY_MODE directly so no real Milvus/LLM calls occur.
    SCORER_BY_MODE is looked up at call-time inside score_direct_criteria so
    patching the dict is the correct interception point.
    """
    import uuid
    from unittest.mock import AsyncMock, patch
    import app.services.criterion_scorers as _scorers_mod

    cid_exp  = str(uuid.uuid4())
    cid_plan = str(uuid.uuid4())
    cid_req  = str(uuid.uuid4())
    cid_par  = str(uuid.uuid4())

    criteria = [
        {
            "criterion_id":   cid_exp,
            "label":          "Portfolio of similar projects",
            "section":        "Company Profile",
            "weight":         15.0,
            "max_score":      5,
            "criterion_type": "leaf",
            "evaluation_mode": "experience_references",
        },
        {
            "criterion_id":   cid_plan,
            "label":          "Project implementation plan",
            "section":        "Proposed Solution",
            "weight":         10.0,
            "max_score":      5,
            "criterion_type": "leaf",
            "evaluation_mode": "plan_methodology",
        },
        {
            "criterion_id":   cid_req,
            "label":          "Functional requirements",
            "section":        "Proposed Services",
            "weight":         25.0,
            "max_score":      5,
            "criterion_type": "leaf",
            "evaluation_mode": "requirement_coverage",   # must NOT be dispatched
        },
        {
            "criterion_id":   cid_par,
            "label":          "Parent node",
            "section":        "",
            "weight":         0.0,
            "max_score":      5,
            "criterion_type": "parent",                  # must NOT be dispatched
            "evaluation_mode": None,
        },
    ]

    mock_exp  = AsyncMock(return_value={"criterion_id": cid_exp,  "score": 3.5, "raw_score": 3.5, "req_id": None})
    mock_plan = AsyncMock(return_value={"criterion_id": cid_plan, "score": 4.0, "raw_score": 4.0, "req_id": None})

    # Patch SCORER_BY_MODE dict — score_direct_criteria looks up from it at call-time
    patched_map = {
        "experience_references": mock_exp,
        "plan_methodology":      mock_plan,
        "team_capability":       AsyncMock(return_value=None),
        "solution_quality":      AsyncMock(return_value=None),
        "commercial":            AsyncMock(return_value=None),
    }
    with patch.object(_scorers_mod, "SCORER_BY_MODE", patched_map):
        results = await _scorers_mod.score_direct_criteria("rfp-1", "resp-1", criteria)

    assert len(results) == 2, f"Expected 2 results (req_coverage and parent excluded), got {len(results)}"
    mock_exp.assert_called_once()
    mock_plan.assert_called_once()

    cids = {r["criterion_id"] for r in results}
    assert cid_exp  in cids, "experience_references criterion missing from results"
    assert cid_plan in cids, "plan_methodology criterion missing from results"
    assert cid_req  not in cids, "requirement_coverage must not be dispatched to direct scorers"
    assert cid_par  not in cids, "parent node must not be dispatched"
