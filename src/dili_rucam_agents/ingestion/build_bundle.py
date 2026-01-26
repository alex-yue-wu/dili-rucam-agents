from __future__ import annotations

import json
from pathlib import Path
from typing import List

from crewai.tools import BaseTool

from .case_bundle import CaseBundle, CaseBundleBlock, CaseBundleTable, QualityMetrics, merge_blocks
from .pdfplumber_tables import extract_tables
from .pymupdf_fallback import extract_fallback_blocks
from .unstructured_ingest import run_unstructured_ingest


class CaseBundleExtractionError(RuntimeError):
    pass


def build_case_bundle(pdf_path: Path) -> CaseBundle:
    if not pdf_path.exists():
        raise CaseBundleExtractionError(f"PDF not found: {pdf_path}")

    blocks: List[CaseBundleBlock] = []
    notes: List[str] = []

    unstructured_blocks, unstructured_notes = run_unstructured_ingest(pdf_path)
    blocks.extend(unstructured_blocks)
    notes.extend(unstructured_notes)

    fallback_blocks: List[CaseBundleBlock] = []
    if not blocks:
        fallback_blocks, fallback_notes = extract_fallback_blocks(pdf_path)
        blocks.extend(fallback_blocks)
        notes.extend(fallback_notes)
    else:
        fb_blocks, fb_notes = extract_fallback_blocks(pdf_path)
        fallback_blocks = fb_blocks
        notes.extend(fb_notes)

    tables, table_notes = extract_tables(pdf_path)
    notes.extend(table_notes)

    deduped_blocks = merge_blocks(blocks, fallback_blocks)
    normalized_text = "\n".join(block.text for block in deduped_blocks if block.text).strip()

    quality = QualityMetrics(
        unstructured_total_score=len(unstructured_blocks),
        fallback_pages=[block.page_number for block in fallback_blocks],
        fallback_total_score=len(fallback_blocks),
    )

    return CaseBundle(
        pdf_path=str(pdf_path),
        extraction_notes=notes,
        blocks=deduped_blocks,
        normalized_text=normalized_text,
        tables=tables,
        unknowns=[],
        quality=quality,
    )


class CaseBundleExtractionTool(BaseTool):
    """CrewAI tool wrapper around the deterministic case bundle pipeline."""

    name: str = "case_bundle_extractor"
    description: str = (
        "Convert a PDF path into the canonical case_bundle_json contract "
        "defined in agent.md using deterministic ingestion."
    )

    def _run(self, pdf_path: str) -> str:
        bundle = build_case_bundle(Path(pdf_path))
        return json.dumps(bundle.to_dict(), indent=2)

    async def _arun(self, pdf_path: str) -> str:  # pragma: no cover - async parity
        return self._run(pdf_path)


__all__ = [
    "CaseBundleExtractionError",
    "CaseBundleExtractionTool",
    "build_case_bundle",
]
