"""
Classify RFP ZIP entries for matrix (deterministic Excel) vs document (LLM) pipelines.
"""
from pathlib import Path

# ── Evaluation matrix detection ───────────────────────────────────────────────
# The user-spec guarantees: evaluation matrix filenames always contain "evaluation".
_EVAL_MATRIX_XLSX_EXTS = frozenset({".xlsx", ".xlsm"})


def is_evaluation_matrix_workbook(file_name: str) -> bool:
    """
    Return True when a file is an evaluation-criteria matrix (as opposed to a
    compliance/requirements matrix).

    Detection rule: filename contains 'evaluation' (case-insensitive) AND has
    an Excel extension.  This matches the project spec guarantee that evaluation
    matrix files always carry the word 'evaluation' in their name.
    """
    stem = Path(file_name).name.lower()
    ext  = Path(file_name).suffix.lower()
    return ext in _EVAL_MATRIX_XLSX_EXTS and "evaluation" in stem

# Worksheet names that are never official requirement matrices
_SKIP_MATRIX_SHEET_KEYWORDS = (
    "instruction",
    "instructions",
    "evaluation",
    "eval ",
    " eval",
    "scoring",
    "scorecard",
    "score sheet",
    "criteria",
    "rubric",
    "readme",
    "cover",
    "legend",
    "toc",
    "contents",
    "summary",
    "overview",
    "guidance",
    "notes",
    "template",
)

_COMPLIANCE_MATRIX_KEYWORDS = (
    "compliance",
    "matrix",
    "requirement",
    "req_matrix",
    "req matrix",
    "spec matrix",
)


def should_skip_matrix_sheet(sheet_name: str) -> bool:
    n = sheet_name.lower().strip()
    if not n:
        return True
    for kw in _SKIP_MATRIX_SHEET_KEYWORDS:
        if kw in n:
            return True
    if n in ("eval", "evaluation", "instructions", "instruction"):
        return True
    return False


def is_non_matrix_workbook(file_name: str, doc_role: str) -> bool:
    """Evaluation / instruction Excel files — used for criteria, not requirement matrix."""
    return False


def is_compliance_matrix_workbook(file_name: str, doc_role: str) -> bool:
    """Any Excel workbook may contain the official matrix rows; let the parser decide."""
    ext = Path(file_name).suffix.lower()
    if ext not in (".xlsx", ".xlsm"):
        return False
    return True


def is_llm_document_source(file_name: str) -> bool:
    """Document requirements (Dataset B) come only from prose files, never Excel."""
    return Path(file_name).suffix.lower() in (".pdf", ".docx", ".doc")
