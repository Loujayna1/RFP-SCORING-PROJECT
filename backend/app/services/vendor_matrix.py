"""
Reading of the compliance matrix returned by a vendor.

A vendor returns the RFP's compliance matrix with, for each requirement, its
own compliance statement (for example "Fully Compliant", "PC" or "Yes"), a
detailed response, and sometimes a reference to a document and page.

These declarations are DATA, not evidence. The declared level is the claim
that the system must verify; if it were indexed with the vendor's documents,
the language model could read the vendor's own statement and repeat it instead
of judging the evidence. This module therefore separates the two:

* read_vendor_declarations() returns one record per requirement with the
  declared level, the response text and the reference, which are stored in
  the vendor_declaration table for later comparison with the verified level;
* evidence_text_for() builds the text that may be indexed as evidence: the
  requirement and the vendor's detailed response, with every compliance
  statement removed.
"""
from __future__ import annotations

import io
import logging
import re
from typing import Dict, List, Optional

import openpyxl

from app.services.matrix_processor import extract_requirements_from_xlsx

logger = logging.getLogger(__name__)

# Canonical levels, aligned with the levels used by the scoring engine.
FULLY, PARTIALLY, ROADMAP, NON = (
    "FullyCompliant", "PartiallyCompliant", "RoadmapCompliant", "NonCompliant",
)

_LEVEL_PATTERNS = [
    (ROADMAP,   r"^(roadmap(\s+compliant)?|rc|planned|future\s+release|to\s+be\s+developed|tbd|in\s+development|on\s+roadmap)$"),
    (PARTIALLY, r"^(partially(\s+compliant)?|partial(ly)?|pc|partiellement(\s+conforme)?)$"),
    (NON,       r"^(non[\s-]*compliant|not\s+compliant|nc|no|non|non[\s-]*conforme|not\s+supported)$"),
    (FULLY,     r"^(fully(\s+compliant)?|compliant|fc|yes|oui|conforme|full(y)?\s+supported)$"),
]

# Header words that identify each column of a returned matrix.
_DECLARED_HEADER = ("complian", "provider", "yes / no", "yes/no", "conformit", "oui / non", "status")
_RESPONSE_HEADER = ("response", "comment", "detail", "justification", "answer", "explanation")
_REFERENCE_HEADER = ("reference", "ref.", "document", "page", "source")

# Compliance statements removed from response text before it is indexed.
_LEVEL_MENTION_RE = re.compile(
    r"\b(fully|partially|partly|roadmap|non|not)[\s-]*(compliant|conforme)\b"
    r"|\b(FC|PC|RC|NC)\b(?=\s*[:.\-–—,)]|\s*$)"
    r"|^\s*(yes|no|oui|non)\b\s*[:.\-–—,]?",
    re.IGNORECASE,
)


def normalise_level(value) -> Optional[str]:
    """Map a vendor's compliance statement to a canonical level, or None."""
    if value is None:
        return None
    t = re.sub(r"[\s_]+", " ", str(value)).strip().lower().rstrip(".")
    if not t:
        return None
    for level, pattern in _LEVEL_PATTERNS:
        if re.match(pattern, t):
            return level
    return None


def strip_level_mentions(text: str) -> str:
    """Remove compliance statements from a vendor's response text."""
    cleaned = _LEVEL_MENTION_RE.sub("", text or "")
    return re.sub(r"\s{2,}", " ", cleaned).strip(" .:-–—,")


def _cell(v) -> str:
    return "" if v is None else str(v).strip()


def _header_columns(row_values) -> Dict[str, int]:
    """Locate the declared, response and reference columns in a header row."""
    cols: Dict[str, int] = {}
    for idx, v in enumerate(row_values):
        t = _cell(v).lower()
        if not t or len(t) > 60:
            continue
        if "declared" not in cols and any(k in t for k in _DECLARED_HEADER):
            cols["declared"] = idx
        elif "response" not in cols and any(k in t for k in _RESPONSE_HEADER):
            cols["response"] = idx
        elif "reference" not in cols and any(k in t for k in _REFERENCE_HEADER) and " id" not in t:
            cols["reference"] = idx
    return cols


def read_vendor_declarations(file_bytes: bytes, filename: str) -> List[Dict]:
    """
    Read the declarations of a returned compliance matrix.

    Requirements are located with the same reader used for the RFP's own
    matrices, so codes (given or generated) match those of the RFP. For each
    requirement, the answer columns are read from the closest header row above
    it. Returns an empty list when the workbook is not a filled matrix.
    """
    requirements = extract_requirements_from_xlsx(file_bytes, filename)["requirements"]
    if not requirements:
        return []

    try:
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
    except Exception as exc:
        logger.warning("Cannot open vendor workbook %s: %s", filename, exc)
        return []

    requirement_rows: Dict[str, set] = {}
    for req in requirements:
        requirement_rows.setdefault(req.get("source_sheet"), set()).add(req.get("source_row"))

    header_rows: Dict[str, List[tuple]] = {}
    for ws in wb.worksheets:
        rows = []
        for r_idx, row in enumerate(ws.iter_rows(values_only=True), start=1):
            # A requirement row is never a header, even when its answer
            # ("Fully Compliant") contains a header word.
            if r_idx in requirement_rows.get(ws.title, set()):
                continue
            if any(normalise_level(v) for v in row if v is not None):
                continue
            filled = [i for i, v in enumerate(row) if _cell(v)]
            if len(filled) < 2:
                continue                      # a title row, not a table header
            cols = _header_columns(row)
            if "declared" in cols and cols["declared"] > filled[0]:
                rows.append((r_idx, cols))
        header_rows[ws.title] = rows

    declarations: List[Dict] = []
    for req in requirements:
        sheet, row_no = req.get("source_sheet"), req.get("source_row")
        if sheet not in wb.sheetnames or not row_no:
            continue
        candidates = [cols for r, cols in header_rows.get(sheet, []) if r < row_no]
        if not candidates:
            continue
        cols = candidates[-1]
        row = [c.value for c in wb[sheet][row_no]]

        def _get(key: str) -> str:
            i = cols.get(key)
            return _cell(row[i]) if i is not None and i < len(row) else ""

        declared_raw = _get("declared")
        if declared_raw and declared_raw.strip() == str(req.get("original_text", "")).strip():
            declared_raw = ""          # merged requirement cell, not an answer
        response = _get("response")
        if not declared_raw and not response:
            continue
        declarations.append({
            "req_code": req["req_code"],
            "source_sheet": sheet,
            "source_row": row_no,
            "requirement_text": req["description"],
            "declared_raw": declared_raw,
            "declared_level": normalise_level(declared_raw),
            "response_text": response,
            "reference": _get("reference"),
            "source_file": filename,
        })
    wb.close()

    recognised = sum(1 for d in declarations if d["declared_level"])
    if recognised < max(3, 0.3 * len(requirements)):
        # Not a filled compliance matrix (e.g. a pricing sheet or an empty template).
        return []
    logger.info("Vendor matrix %s: %d declarations (%d with a recognised level)",
                filename, len(declarations), recognised)
    return declarations


def evidence_text_for(declaration: Dict) -> str:
    """Text of a declaration that may be indexed as evidence (no declared level)."""
    response = strip_level_mentions(declaration.get("response_text", ""))
    if not response:
        return ""
    return f"{declaration['req_code']} {declaration['requirement_text']}\nVendor response: {response}"


_PAGE_RE = re.compile(r"\b(?:p|pp|page|pages)\.?\s*(\d{1,4})", re.IGNORECASE)


def reference_page(reference: Optional[str]) -> Optional[int]:
    """First page number given in a vendor's reference ('..., §5.3, p. 3' -> 3)."""
    m = _PAGE_RE.search(reference or "")
    return int(m.group(1)) if m else None

