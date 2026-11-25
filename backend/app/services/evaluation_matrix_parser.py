"""
Deterministic parser for hierarchical weighted evaluation matrix XLSX files.

Contract
--------
* Only uses openpyxl (no LLM) for structure detection.
* Identifies the sheet by looking for sheets whose name contains "evaluation"
  (case-insensitive); falls back to all sheets if none match.
* Parses hierarchical numeric codes (1 / 1.1 / 1.1.1 ...).
* Assigns evaluation_mode to every leaf criterion via criterion_classifier.
* Returns a nested-tree list compatible with persist_rfp_extraction's
  add_criteria() recursive helper — i.e. dicts with optional "children" lists.

Raises EvaluationMatrixParseError on any structural failure so callers can
surface a clear error instead of silently falling back to a generic rubric.
"""

import io
import re
import logging
from typing import Optional

import openpyxl

from app.services.criterion_classifier import classify_evaluation_mode

logger = logging.getLogger(__name__)


class EvaluationMatrixParseError(Exception):
    """Raised when the evaluation matrix cannot be deterministically parsed."""


# ── Column-detection helpers ──────────────────────────────────────────────────

_HIER_CODE_RE = re.compile(r"^\d+(\.\d+)*$")

_WEIGHT_HEADER_TOKENS = frozenset({
    "poid", "poids",
    "weight", "weights", "weighting", "ponderation", "pondération",
    "coeff", "coefficient", "score", "max", "maximum", "note",
    "pond", "points", "percent", "percentage", "pts", "total", "%",
})
_LABEL_HEADER_TOKENS = frozenset({
    "criterion", "criteria", "label", "description", "item",
    "designation", "libellé", "libelle", "rubrique", "category",
    "section", "name", "evaluation criteria", "eval criteria",
    "evaluation criterion",
})
_CODE_HEADER_TOKENS = frozenset({
    "code", "num", "no", "no.", "number", "#", "ref", "id", "n°",
    "item no", "item #", "n", "numéro",
})


def _cell_str(cell) -> str:
    if cell is None:
        return ""
    if isinstance(cell, bool):
        return "TRUE" if cell else "FALSE"
    if isinstance(cell, float):
        return str(int(cell)) if cell == int(cell) else str(cell)
    return str(cell).strip()


def _is_hier_code(text: str) -> bool:
    t = text.strip()
    return bool(t) and bool(_HIER_CODE_RE.match(t))


def _code_depth(code: str) -> int:
    return code.count(".") + 1


def _parent_code_of(code: str) -> Optional[str]:
    return code.rsplit(".", 1)[0] if "." in code else None


def _parse_weight(raw: str) -> Optional[float]:
    """'35%' → 35.0 | '0.35' → 35.0 | '35' → 35.0 | garbage → None"""
    t = raw.strip().rstrip("%").strip()
    try:
        val = float(t)
        if 0 < val <= 1.0:
            return round(val * 100.0, 4)
        return round(val, 4)
    except (ValueError, TypeError):
        return None


def _detect_columns(header_row: tuple) -> dict:
    """
    Return a dict with optional keys 'code', 'label', 'weight'
    whose values are 0-based column indices.
    """
    cols: dict[str, int] = {}
    for idx, cell in enumerate(header_row):
        raw = _cell_str(cell).lower().strip()
        if not raw:
            continue
        tokens = set(re.split(r"[\s/_\-]+", raw))

        if "code" not in cols and (tokens & _CODE_HEADER_TOKENS or raw in _CODE_HEADER_TOKENS):
            cols["code"] = idx
        elif "label" not in cols and (tokens & _LABEL_HEADER_TOKENS or raw in _LABEL_HEADER_TOKENS):
            cols["label"] = idx
        elif "weight" not in cols and (tokens & _WEIGHT_HEADER_TOKENS or raw in _WEIGHT_HEADER_TOKENS):
            cols["weight"] = idx
    return cols


def _find_header_row(ws) -> tuple[Optional[int], dict]:
    """Scan the first 25 rows for a header that has at least a label column."""
    for row_idx, row in enumerate(ws.iter_rows(min_row=1, max_row=25, values_only=True), start=1):
        cols = _detect_columns(tuple(row))
        if "label" in cols:
            return row_idx, cols
    return None, {}


# Code glued to its label, as written in real matrices: "1.1.Time engagement",
# "1.3. Functional requirements", "2. References".
_CODE_LABEL_RE = re.compile(r"^\s*(\d+(?:\.\d+)*)\.?\s*(.*?)\s*$")
# Rows that end the criteria table or are not criteria at all.
_STOP_LABEL_RE = re.compile(r"^\s*(total|rank|evaluation criteria|grand total)\b", re.IGNORECASE)


def _split_code_label(text: str) -> tuple[Optional[str], str]:
    """'1.2.Submission quality' -> ('1.2', 'Submission quality'); 'Pricing' -> (None, 'Pricing')."""
    m = _CODE_LABEL_RE.match(text)
    if m and m.group(2) and re.search(r"[A-Za-z]", m.group(2)):
        return m.group(1), m.group(2)
    return None, text.strip()


# ── Sheet parser ──────────────────────────────────────────────────────────────

def _parse_sheet(ws, sheet_name: str) -> list[dict]:
    """
    Parse a worksheet into a flat list of criterion rows, in reading order.

    Each row: {"code", "label", "weight", "level", "row"}.

    The hierarchy is read from the LAYOUT, which is how evaluation matrices are
    actually written: a main criterion sits in the first label column and its
    sub-criteria one column to the right. Numeric codes are kept when present
    ("1.2.Submission quality"), but they are not trusted for structure, because
    real matrices contain numbering mistakes (e.g. "3. Technical resources"
    followed by "4.1", "4.2"). Weight cells are never mistaken for codes.
    """
    header_idx, cols = _find_header_row(ws)
    if header_idx is None:
        raise EvaluationMatrixParseError(
            f"Sheet '{sheet_name}': no header row with a label column found"
        )

    label_col = cols.get("label")
    weight_col = cols.get("weight")
    code_col = cols.get("code")
    if label_col is None:
        raise EvaluationMatrixParseError(
            f"Sheet '{sheet_name}': cannot identify label/description column"
        )
    # Label cells live between the label column and the weight column
    # (or within 4 columns of the label column when no weight column exists).
    last_label_col = (weight_col - 1) if weight_col is not None else label_col + 4

    raw_rows: list[dict] = []
    for row_idx, row in enumerate(ws.iter_rows(min_row=header_idx + 1, values_only=True),
                                  start=header_idx + 1):
        row = tuple(row)
        text, level = "", None
        for ci in range(label_col, min(last_label_col, len(row) - 1) + 1):
            t = _cell_str(row[ci])
            if t and re.search(r"[A-Za-z]", t):
                text, level = t, ci - label_col
                break
        if not text:
            continue                      # score-only or empty row
        if _STOP_LABEL_RE.match(text):
            break                         # end of the criteria table
        code, label = _split_code_label(text)
        if code is None and code_col is not None and code_col < len(row):
            candidate = _cell_str(row[code_col]).strip()
            if _is_hier_code(candidate):
                code = candidate
        if len(label) < 2:
            continue
        weight: Optional[float] = None
        if weight_col is not None and weight_col < len(row):
            weight = _parse_weight(_cell_str(row[weight_col]))
        raw_rows.append({"code": code, "label": label, "weight": weight,
                         "level": level, "row": row_idx})

    if not raw_rows:
        raise EvaluationMatrixParseError(f"Sheet '{sheet_name}': no criteria rows found")
    if all(r["weight"] is None for r in raw_rows):
        raise EvaluationMatrixParseError(f"Sheet '{sheet_name}': no weights found")
    return raw_rows


# ── Tree builder ──────────────────────────────────────────────────────────────

def _build_tree(raw_rows: list[dict]) -> list[dict]:
    """
    Convert flat rows into a nested tree compatible with
    persist_rfp_extraction's add_criteria() recursive helper.

    The parent of a row is the closest preceding row with a smaller layout level.
    When every row sits at the same level (a matrix written in one column), the
    numeric codes are used instead. Each node receives a structural code
    ("1", "1.2", ...) built from its position, so downstream code that relies on
    code prefixes sees a consistent hierarchy even when the printed numbering is
    wrong; the printed code is kept in "_source_code".
    """
    single_level = len({r["level"] for r in raw_rows}) == 1

    nodes: list[dict] = []
    for r in raw_rows:
        nodes.append({
            "label":           r["label"],
            "section":         r["label"],
            "weight":          r["weight"],
            "max_score":       5,
            "criterion_type":  "leaf",
            "evaluation_mode": None,
            "_code":           None,
            "_source_code":    r["code"],
            "_level":          r["level"],
            "children":        [],
        })

    roots: list[dict] = []
    stack: list[dict] = []
    for node in nodes:
        if single_level and node["_source_code"]:
            # Fall back on printed codes: parent = node whose code is our prefix.
            parent = None
            code = node["_source_code"]
            for cand in reversed(stack):
                cc = cand["_source_code"]
                if cc and code.startswith(cc + "."):
                    parent = cand
                    break
            stack.append(node)
        else:
            while stack and stack[-1]["_level"] >= node["_level"]:
                stack.pop()
            parent = stack[-1] if stack else None
            stack.append(node)

        if parent is None:
            roots.append(node)
        else:
            parent["children"].append(node)

    def _finalize(siblings: list[dict], prefix: str, parent_label: Optional[str]) -> None:
        for i, n in enumerate(siblings, start=1):
            n["_code"] = f"{prefix}{i}"
            n["section"] = parent_label or n["label"]
            if n["children"]:
                n["criterion_type"] = "parent"
                n["evaluation_mode"] = None
                _finalize(n["children"], n["_code"] + ".", n["label"])
            else:
                n["criterion_type"] = "leaf"
                n["evaluation_mode"] = classify_evaluation_mode(n["label"], parent_label)

    _finalize(roots, "", None)
    for n in nodes:
        n.pop("_level", None)
    return roots


# ── Public API ────────────────────────────────────────────────────────────────

def parse_evaluation_matrix_xlsx(file_bytes: bytes, filename: str) -> list[dict]:
    """
    Parse a hierarchical weighted evaluation matrix XLSX into a criteria tree.

    Returns
    -------
    A nested list of criterion dicts suitable for persist_rfp_extraction's
    add_criteria() function.  Leaf nodes have evaluation_mode set; parent
    nodes have criterion_type='parent'.

    Raises
    ------
    EvaluationMatrixParseError
        If the file cannot be parsed into a valid criterion hierarchy.
        Callers must NOT silently substitute a default rubric.
    """
    try:
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True, read_only=False)
    except Exception as exc:
        raise EvaluationMatrixParseError(f"Cannot open workbook '{filename}': {exc}") from exc

    # Try sheets with 'evaluation' in the name first; fall back to all sheets
    candidate_sheets = [n for n in wb.sheetnames if "evaluation" in n.lower()]
    if not candidate_sheets:
        candidate_sheets = list(wb.sheetnames)

    last_error: Optional[EvaluationMatrixParseError] = None
    for sheet_name in candidate_sheets:
        ws = wb[sheet_name]
        try:
            raw_rows = _parse_sheet(ws, sheet_name)
        except EvaluationMatrixParseError as exc:
            last_error = exc
            logger.debug("Sheet '%s' in '%s': %s", sheet_name, filename, exc)
            continue

        tree = _build_tree(raw_rows)
        wb.close()

        flat = flat_criteria_from_tree(tree)
        leaf_count = sum(1 for n in flat if n["criterion_type"] == "leaf")
        logger.info(
            "Parsed eval matrix '%s' sheet '%s': %d criteria (%d leaf, %d parent)",
            filename, sheet_name, len(flat), leaf_count, len(flat) - leaf_count,
        )
        return tree

    wb.close()
    raise EvaluationMatrixParseError(
        f"No valid evaluation matrix found in '{filename}'. "
        f"Last error: {last_error}. "
        "Ensure the workbook contains a sheet with hierarchical numeric codes "
        "(e.g. 1 / 1.1 / 1.1.1) and a label column."
    )


def flat_criteria_from_tree(tree: list[dict]) -> list[dict]:
    """
    Flatten a nested tree back to a list (depth-first).
    Useful for unit-testing the parser output.
    """
    result = []
    def _walk(nodes: list[dict]) -> None:
        for node in nodes:
            result.append(node)
            if node.get("children"):
                _walk(node["children"])
    _walk(tree)
    return result

