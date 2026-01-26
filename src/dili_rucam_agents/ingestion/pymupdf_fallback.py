from __future__ import annotations

from pathlib import Path
from typing import List, Tuple

from .case_bundle import CaseBundleBlock


def extract_fallback_blocks(pdf_path: Path) -> Tuple[List[CaseBundleBlock], List[str]]:
    """Fallback text extraction using PyMuPDF (fitz)."""

    try:
        import fitz  # type: ignore
    except Exception as exc:  # pragma: no cover - dependency optional
        return [], [f"PyMuPDF unavailable: {exc}"]

    blocks: List[CaseBundleBlock] = []
    doc = fitz.open(str(pdf_path))
    for page_index in range(doc.page_count):
        page = doc.load_page(page_index)
        text = page.get_text("text")
        if not text.strip():
            continue

        blocks.append(
            CaseBundleBlock(
                element_type="NarrativeText",
                page_number=page_index + 1,
                text=" ".join(line.strip() for line in text.splitlines() if line.strip()),
            )
        )

    doc.close()
    notes = ["PyMuPDF fallback blocks generated"] if blocks else []
    return blocks, notes
