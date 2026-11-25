"""
Deterministic extraction of official matrix requirements from compliance Excel workbooks.

Ground-truth rules:
- One category per worksheet (sheet name = category).
- Cell values copied verbatim; only mandatory is interpreted as metadata.
- Instruction / evaluation sheets and non-matrix workbooks are excluded.
"""
import io
import logging
import re
from typing import Any, Dict, List, Optional, Tuple, TypedDict

import openpyxl

from app.services.rfp_document_routing import should_skip_matrix_sheet

logger = logging.getLogger(__name__)

_MIN_DESC_LEN = 12
_CODE_PATTERN = re.compile(
    r"^[A-Z]{1,5}(\.[A-Z0-9]{1,5}){1,4}\.?\d*$",
    re.IGNORECASE,
)
_CODE_PATTERN_ALT = re.compile(r"^[A-Z0-9][A-Z0-9._/-]{2,39}$", re.IGNORECASE)

_HEADER_LABELS = frozenset({
    "requirement", "requirements", "description", "specification",
    "mandatory", "category", "code", "id", "ref", "#", "no", "no.",
    "number", "num", "item", "priority", "prio", "essential", "type",
    "requirement id", "req id", "requirement text", "requirement description",
    "total", "summary",
})

_NON_REQUIREMENT_PHRASES = frozenset({"instructions", "instruction"})
_NON_REQUIREMENT_PREFIXES = (
    "the vendor shall only use these compliance statements",
    "all items having such compliance statements",
    "the vendor is invited to provide",
)


class MatrixExtractionResult(TypedDict):
    requirements: List[Dict]
    sections: List[Dict]


def _verbatim_cell(cell: Any) -> str:
    if cell is None:
        return ""
    if isinstance(cell, bool):
        return "TRUE" if cell else "FALSE"
    if isinstance(cell, (int, float)):
        if isinstance(cell, float) and cell == int(cell):
            return str(int(cell))
        return str(cell)
    return str(cell).strip()


def _cell_lower(cell: Any) -> str:
    return _verbatim_cell(cell).lower()


def _looks_like_req_code(text: str) -> bool:
    t = text.strip()
    if not t or len(t) > 40:
        return False
    if _CODE_PATTERN.match(t):
        return True
    # A requirement code mixes letters and digits ("SEC-04", "R12"). A bare
    # number ("450", "1250") is a quantity or a price, not an identifier.
    if _CODE_PATTERN_ALT.match(t) and any(ch.isdigit() for ch in t) and any(ch.isalpha() for ch in t):
        return True
    if "." in t and len(t) < 28 and t.count(".") >= 2 and " " not in t:
        return True
    return False


def _find_req_code_in_row(row_vals: Tuple[Any, ...], code_col: Optional[int]) -> Tuple[Optional[int], Optional[str]]:
    if code_col is not None and code_col < len(row_vals):
        preferred = _verbatim_cell(row_vals[code_col])
        if _looks_like_req_code(preferred):
            return code_col, preferred

    for idx, cell in enumerate(row_vals):
        if code_col is not None and idx == code_col:
            continue
        value = _verbatim_cell(cell)
        if _looks_like_req_code(value):
            return idx, value

    return None, None


def _pick_requirement_text(
    row_vals: Tuple[Any, ...],
    *,
    req_code: str,
    req_code_col: Optional[int],
    desc_col: Optional[int],
    category_label: str,
) -> Optional[str]:
    def valid_text(value: str) -> bool:
        t = value.strip()
        if len(t) < _MIN_DESC_LEN:
            return False
        if _looks_like_req_code(t):
            return False
        if _looks_like_heading_row(t, category_label):
            return False
        if _is_header_label(t):
            return False
        return True

    if desc_col is not None and desc_col < len(row_vals):
        preferred = _verbatim_cell(row_vals[desc_col])
        if preferred and preferred.strip().lower() != req_code.lower() and valid_text(preferred):
            return preferred

    candidates: List[str] = []
    for idx, cell in enumerate(row_vals):
        if req_code_col is not None and idx == req_code_col:
            continue
        value = _verbatim_cell(cell)
        if not value:
            continue
        if value.strip().lower() == req_code.lower():
            continue
        if valid_text(value):
            candidates.append(value)

    if not candidates:
        return None

    return max(candidates, key=lambda s: len(s.strip()))


def _is_header_label(text: str) -> bool:
    t = text.lower().strip()
    if not t:
        return True
    if t in _HEADER_LABELS:
        return True
    if len(t) < 35 and t.endswith(" id"):
        return True
    return False


def _detect_header_columns(row_vals: Tuple[Any, ...]) -> Optional[Dict[str, int]]:
    row_lower = [_cell_lower(c) for c in row_vals]
    headers: Dict[str, int] = {}

    # Pass 1 — code / ID columns (before requirement text to avoid "Requirement ID" → desc)
    for col_idx, v in enumerate(row_lower):
        if not v:
            continue
        if v in (
            "id", "#", "ref", "code", "no.", "no", "number", "num",
            "item", "item no", "item #", "req id", "requirement id",
            "requirement no", "requirement #", "ref no",
        ):
            headers["code"] = col_idx
            continue
        if "requirement id" in v or "req id" in v or "req. id" in v:
            headers["code"] = col_idx
            continue
        if v.endswith(" id") and ("req" in v or "requirement" in v):
            headers["code"] = col_idx

    # Pass 2 — requirement text column (not the ID column)
    for col_idx, v in enumerate(row_lower):
        if not v or col_idx == headers.get("code"):
            continue
        if v in ("requirement", "requirements", "requirement text", "requirement description"):
            headers["desc"] = col_idx
            break
        if "description" in v or "specification" in v or "statement of work" in v:
            headers["desc"] = col_idx
            break
        if "requirement" in v and " id" not in v and not v.endswith(" id"):
            if "code" not in v and "ref" not in v.split():
                headers["desc"] = col_idx
                break

    if "desc" not in headers:
        return None

    desc_col = headers["desc"]
    for col_idx, v in enumerate(row_lower):
        if not v or col_idx == desc_col:
            continue
        if "mandatory" not in headers:
            if any(x in v for x in ("mandatory", "essential", "must have")) or v in ("m", "must", "m/o"):
                headers["mandatory"] = col_idx
            elif v in ("prio", "priority"):
                headers["mandatory"] = col_idx
        if "category" not in headers:
            if any(x in v for x in ("category", "section", "domain", "pillar", "stream", "area")):
                headers["category"] = col_idx

    if "code" not in headers and desc_col > 0:
        left = _verbatim_cell(row_vals[desc_col - 1])
        if left and _looks_like_req_code(left):
            headers["code"] = desc_col - 1

    return headers


def _refine_column_mapping(
    ws,
    header_row_idx: int,
    headers: Dict[str, int],
) -> Optional[Dict[str, int]]:
    """If description column is mostly IDs, pick the column with longer prose text."""
    desc_col = headers["desc"]
    code_col = headers.get("code")
    samples: List[Tuple[Any, ...]] = []
    for row in ws.iter_rows(
        min_row=header_row_idx + 1,
        max_row=header_row_idx + 15,
        values_only=True,
    ):
        samples.append(tuple(row))
    if not samples:
        return headers

    max_cols = max(len(r) for r in samples)

    def column_stats(cidx: int) -> Tuple[int, float, int]:
        vals = [
            _verbatim_cell(r[cidx]) if cidx < len(r) else ""
            for r in samples
        ]
        vals = [v for v in vals if v and not _is_header_label(v)]
        if not vals:
            return 0, 0.0, 0
        code_like = sum(1 for v in vals if _looks_like_req_code(v))
        avg_len = sum(len(v) for v in vals) / len(vals)
        return code_like, avg_len, len(vals)

    d_codes, d_avg, d_n = column_stats(desc_col)
    if d_n and d_codes >= max(2, d_n // 2) and d_avg < 35:
        best_col = None
        best_avg = 0.0
        for cidx in range(max_cols):
            if cidx == code_col or cidx == desc_col:
                continue
            cl, al, n = column_stats(cidx)
            if n < 2 or cl >= n:
                continue
            if al > best_avg and al >= _MIN_DESC_LEN:
                best_avg = al
                best_col = cidx
        if best_col is not None:
            if code_col is None:
                headers["code"] = desc_col
            headers["desc"] = best_col
            logger.debug(
                "Refined matrix columns: code=%s desc=%s (was desc=%s)",
                headers.get("code"),
                best_col,
                desc_col,
            )

    return headers


def _find_header_row(ws) -> Tuple[Optional[int], Optional[Dict[str, int]], List[str]]:
    best_row_idx: Optional[int] = None
    best_headers: Optional[Dict[str, int]] = None
    best_score = -1
    header_labels: List[str] = []

    for row_idx, row in enumerate(ws.iter_rows(min_row=1, max_row=45, values_only=True), start=1):
        # The header is always above the requirements: once a row carries a
        # requirement code, stop. Without this, a requirement whose text
        # contains "description" or "mandatory" was taken as the header and
        # every requirement above it was lost.
        if best_row_idx is not None and _find_req_code_in_row(tuple(row), None)[1]:
            break
        row = tuple(c if not (isinstance(c, str) and len(c.strip()) > 60) else None for c in row)
        detected = _detect_header_columns(tuple(row))
        if not detected or "desc" not in detected:
            continue
        score = 3
        if "code" in detected:
            score += 3
        if "mandatory" in detected:
            score += 1
        if score > best_score:
            best_score = score
            best_row_idx = row_idx
            best_headers = detected
            header_labels = [
                _cell_lower(c) for c in row if c is not None and _verbatim_cell(c)
            ]

    if best_row_idx is None or best_headers is None:
        return None, None, []

    refined = _refine_column_mapping(ws, best_row_idx, best_headers)
    return best_row_idx, refined, header_labels


def _mandatory_from_cell(mandatory: Any) -> bool:
    if mandatory is None:
        return True
    ms = _cell_lower(mandatory)
    if ms in ("no", "optional", "o", "n", "false", "0", "na", "n/a", "info", "i"):
        return False
    return True


def _looks_like_heading_row(text: str, category_label: str) -> bool:
    """Skip worksheet titles and other non-requirement header lines."""
    t = text.lower().strip()
    if not t:
        return True
    if t in _NON_REQUIREMENT_PHRASES:
        return True
    if any(t.startswith(prefix) for prefix in _NON_REQUIREMENT_PREFIXES):
        return True
    category = category_label.lower().strip()
    if category and t == category:
        return True
    if category and t == f"{category} requirements":
        return True
    if t.endswith(" requirements") and len(t.split()) <= 3:
        return True
    return False


def _extract_sheet(ws, sheet_name: str, sheet_index: int, source_filename: str) -> List[Dict]:
    header_row_idx, headers, header_labels = _find_header_row(ws)

    data_start = header_row_idx + 1 if headers and header_row_idx is not None else 1
    out: List[Dict] = []
    desc_col = headers.get("desc") if headers else None
    code_col = headers.get("code") if headers else None
    mand_col = headers.get("mandatory") if headers else None
    category_label = sheet_name.strip()
    header_label_set = set(header_labels) | _HEADER_LABELS

    row_order = 0
    for sheet_row, row in enumerate(ws.iter_rows(min_row=data_start, values_only=True), start=data_start):
        row = tuple(row)
        if not row:
            continue

        req_code_col, req_code = _find_req_code_in_row(row, code_col)
        if not req_code:
            continue

        desc_s = _pick_requirement_text(
            row,
            req_code=req_code,
            req_code_col=req_code_col,
            desc_col=desc_col,
            category_label=category_label,
        )
        if not desc_s:
            continue

        dl = desc_s.lower()
        if dl in header_label_set or _is_header_label(desc_s):
            continue
        if dl in ("total", "summary", "subtotal", "grand total"):
            continue

        mandatory_cell = row[mand_col] if mand_col is not None and mand_col < len(row) else None

        out.append(
            {
                "req_code": req_code,
                "description": desc_s,
                "original_text": desc_s,
                "category": category_label,
                "is_mandatory": _mandatory_from_cell(mandatory_cell),
                "req_type": "matrix",
                "source_page": 1,
                "source_sheet": sheet_name,
                "source_file": source_filename,
                "matrix_row_order": row_order,
                "matrix_sheet_order": sheet_index,
                "source_row": sheet_row,          # not persisted; used to read vendor answers
            }
        )
        row_order += 1

    return out


# ── Matrices without requirement codes ────────────────────────────────────────
# Some RFP templates list requirements as plain rows under bold sub-section
# titles, in tables whose header announces the vendor's answer columns
# ("Criterias | Provider (Yes / No) | Comments / details"). Rows have no
# identifier, so a stable one is generated from the sheet, section and order.

_ANSWER_HEADER_TOKENS = (
    "yes / no", "yes/no", "provider", "compliance", "compliancy", "complies",
    "comment", "response", "answer", "conformit", "oui / non", "oui/non",
)
_NON_COMPLIANCE_TABLE_TOKENS = ("price", "prix", "unit price", "quantity", "quantité", "tnd", "cost")
_SECTION_ROW_RE = re.compile(r"^\s*(\d+)\s*[.)]\s*\S")
_PART_ROW_RE = re.compile(r"^\s*[A-Z]\s*\.\s+\S")          # "A.  Business specifications"


def _sheet_prefix(sheet_name: str) -> str:
    words = re.findall(r"[A-Za-z]+", sheet_name)
    letters = "".join(w[0] for w in words[:3]).upper()
    return letters or "REQ"


def _is_answer_header(cells: List[Tuple[int, str]]) -> bool:
    if len(cells) < 2:
        return False
    others = " | ".join(t.lower() for _, t in cells[1:])
    if any(tok in others for tok in _NON_COMPLIANCE_TABLE_TOKENS):
        return False
    return any(tok in others for tok in _ANSWER_HEADER_TOKENS)


def _extract_sheet_codeless(ws, sheet_name: str, sheet_index: int, source_filename: str) -> List[Dict]:
    out: List[Dict] = []
    prefix = _sheet_prefix(sheet_name)
    section_no = 0
    in_table = False
    label_col: Optional[int] = None
    subsection = ""
    counter = 0

    for row in ws.iter_rows(min_row=1, max_row=ws.max_row):
        cells = [(c.column - 1, _verbatim_cell(c.value)) for c in row if _verbatim_cell(c.value)]
        if not cells:
            continue
        first_col, first_text = cells[0]

        if _is_answer_header(cells):
            in_table, label_col, subsection = True, first_col, ""
            continue

        sec = _SECTION_ROW_RE.match(first_text)
        if (len(cells) == 1 and (sec or _PART_ROW_RE.match(first_text))
                and (label_col is None or first_col < label_col)):
            # A numbered section or a part title ends the current table.
            if sec:
                section_no = int(sec.group(1))
            in_table, subsection = False, ""
            continue

        if not in_table or first_col != label_col:
            continue
        text = first_text.strip()
        if len(text) < 2 or _is_header_label(text):
            continue

        cell_obj = row[first_col]
        if getattr(cell_obj.font, "b", False):
            subsection = text                     # bold row = sub-section title
            continue

        counter += 1
        description = f"{subsection}: {text}" if subsection else text
        out.append(
            {
                "req_code": f"{prefix}.{section_no or 0}.{counter:03d}",
                "description": description,
                "original_text": text,
                "category": sheet_name.strip(),
                "is_mandatory": True,
                "req_type": "matrix",
                "source_page": 1,
                "source_sheet": sheet_name,
                "source_file": source_filename,
                "matrix_row_order": counter - 1,
                "matrix_sheet_order": sheet_index,
                "source_row": row[0].row,         # not persisted; used to read vendor answers
            }
        )
    return out


def extract_requirements_from_xlsx(file_bytes: bytes, filename: str) -> MatrixExtractionResult:
    empty: MatrixExtractionResult = {"requirements": [], "sections": []}
    try:
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True, read_only=False)
    except Exception as e:
        logger.error("Failed to open workbook %s: %s", filename, e)
        return empty

    aggregated: List[Dict] = []
    sections: List[Dict] = []
    try:
        for sheet_index, sheet_name in enumerate(wb.sheetnames):
            if should_skip_matrix_sheet(sheet_name):
                logger.info("Skipping non-matrix sheet %r in %s", sheet_name, filename)
                continue

            ws = wb[sheet_name]
            try:
                sheet_reqs = _extract_sheet(ws, sheet_name, sheet_index, filename)
                if not sheet_reqs:
                    sheet_reqs = _extract_sheet_codeless(ws, sheet_name, sheet_index, filename)
            except Exception as e:
                logger.warning("Sheet %s in %s skipped: %s", sheet_name, filename, e)
                continue
            if not sheet_reqs:
                continue

            sections.append(
                {
                    "sheet_name": sheet_name,
                    "category": sheet_name.strip(),
                    "sheet_index": sheet_index,
                    "count": len(sheet_reqs),
                }
            )
            aggregated.extend(sheet_reqs)
            logger.info(
                "Matrix sheet %r in %s: %d rows",
                sheet_name,
                filename,
                len(sheet_reqs),
            )
    finally:
        try:
            wb.close()
        except Exception:
            pass

    logger.info(
        "Matrix %s: %d requirement sheets, %d rows",
        filename,
        len(sections),
        len(aggregated),
    )
    return {"requirements": aggregated, "sections": sections}

