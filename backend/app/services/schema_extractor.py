import openpyxl
import re
import io
import zipfile
import logging
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)

def extract_schema_from_bytes(file_bytes: bytes, filename: str) -> List[Dict]:
    """
    Core logic to extract hierarchical schema from an open Excel workbook.
    Based on user-provided pattern:
    - Finds text cells with numeric prefixes (e.g. "1.", "1.1").
    - Finds weights (floats between 0 and 1).
    - Detects depth from prefix.
    """
    try:
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True, read_only=True)
        # Try to find a sheet with "eval" or "score" in name, fallback to active
        ws = wb.active
        for sheetname in wb.sheetnames:
            if "eval" in sheetname.lower() or "score" in sheetname.lower() or "matrix" in sheetname.lower():
                ws = wb[sheetname]
                break

        tree = []
        current_top_parent = None

        for row in ws.iter_rows(values_only=True):
            cells = [c for c in row if c is not None]
            if not cells:
                continue

            text_parts = []
            weight = None
            source_hint = None

            for cell in row:
                if cell is None:
                    continue
                
                if isinstance(cell, str):
                    val = str(cell).strip()
                    if val:
                        text_parts.append(val)
                
                # Weight detection: 0.0-1.0 OR 1.0-100.0 (as percent)
                if isinstance(cell, (float, int)):
                    val = float(cell)
                    if 0 < val <= 1.0 and weight is None:
                        weight = val
                    elif 1.0 < val <= 100.0 and weight is None:
                        weight = val / 100.0 # Normalize percentage

            if not text_parts or weight is None:
                continue

            # Combine text parts to find prefix and label
            # If part 0 is "1.1" and part 1 is "Description", combine them
            full_text = " ".join(text_parts)
            prefix_match = re.match(r'^(\d+(\.\d+)*)', full_text)
            if not prefix_match:
                # Try to see if any individual part starts with a number
                for p in text_parts:
                    prefix_match = re.match(r'^(\d+(\.\d+)*)', p)
                    if prefix_match:
                        full_text = p if len(p) > len(prefix_match.group()) else " ".join(text_parts)
                        break
            
            if not prefix_match:
                continue
            
            prefix_str = prefix_match.group()
            depth = prefix_str.count('.')

            node = {
                "label": full_text,
                "weight": weight,
                "depth": depth,
                "source_file_hint": text_parts[-1] if len(text_parts) > 2 else None,
                "children": []
            }

            if depth == 0:
                current_top_parent = node
                tree.append(node)
            elif depth == 1 and current_top_parent:
                current_top_parent["children"].append(node)
            elif depth > 1:
                if current_top_parent and current_top_parent["children"]:
                    current_top_parent["children"][-1].setdefault("children", []).append(node)

        return tree
    except Exception as e:
        logger.error(f"Failed to extract schema from {filename}: {e}")
        return []

def analyze_rfp_zip_for_schema(file_bytes: bytes) -> List[Dict]:
    """
    Scans a ZIP file or a standalone XLSX for the evaluation matrix and extracts its schema.
    """
    # 1. Try as standalone XLSX
    try:
        # Check magic numbers for XLSX (PK..)
        if file_bytes.startswith(b"PK"):
            # Could be ZIP or XLSX
            try:
                schema = extract_schema_from_bytes(file_bytes, "uploaded_file.xlsx")
                if schema:
                    return schema
            except Exception:
                pass # Not a valid XLSX, try as ZIP
    except Exception:
        pass

    # 2. Try as ZIP
    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as zf:
            xlsx_files = [n for n in zf.namelist() if n.endswith(".xlsx") and not n.startswith("__")]
            
            # Priority 1: Files with keywords in name
            for name in xlsx_files:
                lname = name.lower()
                if any(k in lname for k in ("eval", "score", "criteria", "matrix", "weight")):
                    logger.info(f"Analyzing prioritized file {name} for scoring schema...")
                    data = zf.read(name)
                    schema = extract_schema_from_bytes(data, name)
                    if schema:
                        return schema
            
            # Priority 2: Try any other XLSX file
            for name in xlsx_files:
                lname = name.lower()
                if not any(k in lname for k in ("eval", "score", "criteria", "matrix", "weight")):
                    logger.info(f"Fallback: Analyzing {name} for scoring schema...")
                    data = zf.read(name)
                    schema = extract_schema_from_bytes(data, name)
                    if schema:
                        return schema
        return []
    except Exception as e:
        logger.error(f"Error reading file for schema: {e}")
        return []
