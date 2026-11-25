#!/usr/bin/env python3
"""
Repair matrix requirements for an existing RFP by re-parsing a provided XLSX.

Usage:
  python backend/scripts/repair_matrix.py <rfp_id> /path/to/matrix.xlsx

This script must be run where the backend package is available (e.g. inside
the `rfp_backend` container). It will delete existing `req_type='matrix'`
rows for the RFP and insert newly extracted rows from the workbook.
"""
import asyncio
import sys
import io
from pathlib import Path

from app.services.matrix_processor import extract_requirements_from_xlsx
from app.db.session import AsyncSessionLocal
from sqlalchemy import delete, select

from app.models import db as models_db


async def repair(rfp_id: str, file_path: str):
    p = Path(file_path)
    if not p.exists():
        print(f"File not found: {file_path}")
        return 2

    data = p.read_bytes()
    result = await asyncio.to_thread(extract_requirements_from_xlsx, data, p.name)
    requirements = result.get("requirements", [])

    async with AsyncSessionLocal() as db:
        # Delete previous matrix rows for this RFP
        await db.execute(
            delete(models_db.Requirement).where(
                models_db.Requirement.rfp_id == rfp_id,
                models_db.Requirement.req_type == "matrix",
            )
        )

        for r in requirements:
            db.add(
                models_db.Requirement(
                    req_id=r.get("req_id") or None,
                    rfp_id=rfp_id,
                    req_code=r.get("req_code"),
                    description=r.get("description"),
                    category=r.get("category"),
                    is_mandatory=r.get("is_mandatory", True),
                    source_page=r.get("source_page"),
                    source_file=r.get("source_file"),
                    source_sheet=r.get("source_sheet"),
                    matrix_row_order=r.get("matrix_row_order"),
                    matrix_sheet_order=r.get("matrix_sheet_order"),
                    req_type="matrix",
                    original_text=r.get("original_text"),
                )
            )
        # Update RFP requirement_count
        rfp_row = (
            await db.execute(
                select(models_db.RFPPackage).where(models_db.RFPPackage.rfp_id == rfp_id)
            )
        ).scalar_one_or_none()
        if rfp_row:
            rfp_row.requirement_count = len(requirements)
        await db.commit()

    print(f"Replaced matrix requirements for RFP {rfp_id}: {len(requirements)} rows")
    return 0


def main():
    if len(sys.argv) < 3:
        print("Usage: repair_matrix.py <rfp_id> /path/to/matrix.xlsx")
        return 2
    rfp_id = sys.argv[1]
    file_path = sys.argv[2]
    return asyncio.run(repair(rfp_id, file_path))


if __name__ == "__main__":
    raise SystemExit(main())
