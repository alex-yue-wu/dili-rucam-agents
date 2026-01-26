from __future__ import annotations

from pathlib import Path
from typing import List, Tuple

from .case_bundle import CaseBundleTable


def extract_tables(pdf_path: Path) -> Tuple[List[CaseBundleTable], List[str]]:
    """Extract tables using pdfplumber (best-effort)."""

    try:
        import pdfplumber  # type: ignore
    except Exception as exc:  # pragma: no cover - dependency optional
        return [], [f"pdfplumber unavailable: {exc}"]

    tables: List[CaseBundleTable] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page_index, page in enumerate(pdf.pages, start=1):
            extracted = page.extract_tables()
            for table in extracted:
                rows = [
                    [cell.strip() for cell in row if cell and cell.strip()]
                    for row in table
                ]
                rows = [row for row in rows if row]
                if not rows:
                    continue
                tables.append(
                    CaseBundleTable(
                        page_number=page_index,
                        table_index=len(tables) + 1,
                        raw_rows=rows,
                        preview=" | ".join(rows[0]) if rows else "",
                    )
                )

    return tables, [f"pdfplumber tables extracted={len(tables)}"]
