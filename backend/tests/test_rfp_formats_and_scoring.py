"""
Tests for the fixes made for real RFP formats and for the scoring pipeline:

1. evaluation matrices written with codes glued to labels, a French weight
   header ("Poid") and sub-criteria placed one column to the right;
2. compliance matrices whose header detection was fooled by requirement text,
   and matrices without requirement codes;
3. vendor declarations kept out of the evidence;
4. scoring on the configured scale, with ScoringFailed instead of a default.
No database or network access is needed.
"""
import io
import asyncio

import openpyxl
from openpyxl.styles import Font

from app.services.evaluation_matrix_parser import parse_evaluation_matrix_xlsx, flat_criteria_from_tree
from app.services.matrix_processor import extract_requirements_from_xlsx
from app.services.vendor_matrix import (
    read_vendor_declarations, evidence_text_for, normalise_level, strip_level_mentions,
)
from app.services import llm


def _save(wb) -> bytes:
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ── 1. Evaluation matrix laid out by columns ─────────────────────────────────

def _layout_matrix() -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Technical Evaluation"
    ws["B6"], ws["P6"] = "Criteria", "Poid"
    rows = [
        ("B", "1.Proposed Services", 0.7),
        ("C", "1.1.Functional requirements", 0.5),
        ("C", "1.2. Technical requirements", 0.5),
        ("B", "2.References", 0.2),
        ("C", "2.1.Portfolio with similar projects", 1.0),
        ("B", "3.Technical Resources", 0.1),
        ("C", "4.1.Number of Affected Ressources", 0.5),   # printed numbering is wrong
        ("C", "4.2.Technical Certifications", 0.5),
        ("B", "Total", 1),
    ]
    for i, (col, label, w) in enumerate(rows, start=7):
        ws[f"{col}{i}"], ws[f"P{i}"] = label, w
    ws["R7"] = 0.7          # weight-like numbers elsewhere must not become codes
    ws["C30"] = "1 - Very Low"
    return _save(wb)


def test_layout_matrix_hierarchy_and_weights():
    flat = flat_criteria_from_tree(parse_evaluation_matrix_xlsx(_layout_matrix(), "eval.xlsx"))
    parents = [c for c in flat if c["criterion_type"] == "parent"]
    leaves = [c for c in flat if c["criterion_type"] == "leaf"]
    assert [p["label"] for p in parents] == ["Proposed Services", "References", "Technical Resources"]
    assert len(leaves) == 5
    resources = next(p for p in parents if p["label"] == "Technical Resources")
    assert {c["label"] for c in resources["children"]} == {
        "Number of Affected Ressources", "Technical Certifications"}
    assert all(c["weight"] is not None for c in flat)
    assert not any(c["label"].lower().startswith("total") for c in flat)


def test_misspelled_resources_classified_as_team():
    flat = flat_criteria_from_tree(parse_evaluation_matrix_xlsx(_layout_matrix(), "eval.xlsx"))
    leaf = next(c for c in flat if c["label"] == "Number of Affected Ressources")
    assert leaf["evaluation_mode"] == "team_capability"


# ── 2. Compliance matrices ───────────────────────────────────────────────────

def test_header_not_taken_from_requirement_text():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Functional"
    ws.append(["Functional Requirements"])
    ws.append(["Func. Id", "Requirement Description", "Compliancy", "Detailed Response"])
    ws.append(["BG.FUN.001", "Date of the transaction is mandatory", None, None])
    ws.append(["BG.FUN.002", "The description of the service and the price is mandatory", None, None])
    ws.append(["BG.FUN.003", "The discount amount is shown", None, None])
    reqs = extract_requirements_from_xlsx(_save(wb), "req.xlsx")["requirements"]
    assert [r["req_code"] for r in reqs] == ["BG.FUN.001", "BG.FUN.002", "BG.FUN.003"]


def _codeless_matrix(answers: dict | None = None) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Technical requirements"
    bold = Font(bold=True)
    ws["A1"] = "1. Technical requirements"
    ws["B2"], ws["C2"], ws["D2"] = "Criterias", "Provider (Yes / No)", "Comments / details"
    ws["B3"] = "Hardware & Technology"; ws["B3"].font = bold
    ws["B4"] = "Hypervisor license type"
    ws["B5"] = "Easy scale-out capabilities"
    ws["B6"] = "Security"; ws["B6"].font = bold
    ws["B7"] = "Data protection and privacy measures"
    ws["A9"] = "2. Pricing"
    ws["B10"], ws["C10"], ws["D10"] = "Criterias", "Quantity / Capacity", "Unit Price TND"
    ws["B11"] = "VM pack 1"
    ws["C12"] = 450                                   # a quantity, not a requirement code
    for row, (level, comment) in (answers or {}).items():
        ws[f"C{row}"], ws[f"D{row}"] = level, comment
    return _save(wb)


def test_codeless_matrix_extracted_with_generated_codes():
    reqs = extract_requirements_from_xlsx(_codeless_matrix(), "req.xlsx")["requirements"]
    assert [r["original_text"] for r in reqs] == [
        "Hypervisor license type", "Easy scale-out capabilities", "Data protection and privacy measures"]
    assert reqs[0]["description"] == "Hardware & Technology: Hypervisor license type"
    assert len({r["req_code"] for r in reqs}) == 3


# ── 3. Vendor declarations stay out of the evidence ──────────────────────────

def test_vendor_declarations_and_clean_evidence():
    data = _codeless_matrix({
        4: ("Fully Compliant", "Fully compliant: KVM based, no per-socket licence."),
        5: ("Partially Compliant", "PC - scale-out limited to 8 nodes today."),
        7: ("No", "No, planned for next year."),
    })
    decl = read_vendor_declarations(data, "vendor.xlsx")
    assert [d["declared_level"] for d in decl] == ["FullyCompliant", "PartiallyCompliant", "NonCompliant"]
    for d in decl:
        text = evidence_text_for(d).lower()
        assert "compliant" not in text and not text.split("vendor response:")[1].strip().startswith(("no", "pc"))


def test_empty_template_is_not_a_filled_matrix():
    assert read_vendor_declarations(_codeless_matrix(), "template.xlsx") == []


def test_level_normalisation_and_stripping():
    assert normalise_level("FC") == "FullyCompliant"
    assert normalise_level("Partially compliant") == "PartiallyCompliant"
    assert normalise_level("Roadmap") == "RoadmapCompliant"
    assert normalise_level("Not Compliant") == "NonCompliant"
    assert normalise_level("maybe") is None
    assert strip_level_mentions("Our compliant platform offers PC backups") == "Our compliant platform offers PC backups"


# ── 4. Scoring on the configured scale ───────────────────────────────────────

class _FakeGroq:
    def __init__(self, answers):
        self.answers = list(answers)
        self.temperatures = []

    async def chat_text(self, messages, model=None, max_tokens=None, temperature=None):
        self.temperatures.append(temperature)
        return self.answers.pop(0)


def _run(answers, levels=None):
    fake = _FakeGroq(answers)
    original = llm.get_groq_client
    llm.get_groq_client = lambda: fake
    try:
        result = asyncio.run(llm.score_requirement("req", "R1", "some evidence", 0.8, compliance_levels=levels))
    finally:
        llm.get_groq_client = original
    return result, fake


def test_configured_scale_gives_the_points():
    levels = [{"label": "Fully Compliant", "value": 10}, {"label": "Partially Compliant", "value": 4},
              {"label": "Non-Compliant", "value": 0}]
    result, fake = _run(['{"reasoning": "r", "compliance_level": "PartiallyCompliant", "justification": "j"}'], levels)
    assert result["raw_score"] == 4 and result["compliance_level"] == "PartiallyCompliant"
    assert fake.temperatures == [0]


def test_unusable_answer_is_retried_then_recorded_as_failed():
    result, fake = _run(["not json at all", '{"compliance_level": "Excellent"}'])
    assert result["compliance_level"] == "ScoringFailed" and result["scoring_failed"] is True
    assert len(fake.temperatures) == 2


def test_failed_assessment_not_counted_in_total():
    from app.services.evaluation import aggregate_vendor_score
    crits = [{"criterion_id": "c", "parent_id": None, "label": "1. Func", "weight": 1.0, "criterion_type": "scored"}]
    reqs = [
        {"criterion_id": "c", "raw_score": 5.0, "human_score": None, "compliance_level": "FullyCompliant", "category": ""},
        {"criterion_id": "c", "raw_score": 0.0, "human_score": None, "compliance_level": "ScoringFailed", "category": ""},
    ]
    assert aggregate_vendor_score(reqs, crits)["total_score"] == 5.0
