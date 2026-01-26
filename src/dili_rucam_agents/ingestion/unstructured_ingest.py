from __future__ import annotations

from pathlib import Path
from typing import List, Tuple

from .case_bundle import CaseBundleBlock


def run_unstructured_ingest(pdf_path: Path) -> Tuple[List[CaseBundleBlock], List[str]]:
    """Return layout-aware blocks plus extraction notes from unstructured."""

    try:
        from unstructured.partition.pdf import partition_pdf  # type: ignore
    except Exception as exc:  # pragma: no cover - dependency optional
        return [], [f"unstructured partition skipped: {exc}"]

    def _partition(strategy: str):
        return partition_pdf(
            filename=str(pdf_path),
            strategy=strategy,
            extract_images_in_pdf=False,
            infer_table_structure=True,
            chunking_strategy="by_title",
        )

    elements = []
    notes: List[str] = []

    try:
        elements = _partition("hi_res")
        notes.append("unstructured partition_pdf hi_res strategy")
    except Exception as exc:  # pragma: no cover - environment dependent
        notes.append(f"unstructured hi_res failed: {exc}")
        try:
            elements = _partition("fast")
            notes.append("unstructured partition_pdf fast strategy fallback")
        except Exception as exc2:
            notes.append(f"unstructured fast failed: {exc2}")
            return [], notes

    blocks: List[CaseBundleBlock] = []
    for element in elements:
        metadata = getattr(element, "metadata", None)
        page_number = getattr(metadata, "page_number", 1) if metadata else 1
        text = str(element)
        if not text.strip():
            continue

        blocks.append(
            CaseBundleBlock(
                element_type=element.category if hasattr(element, "category") else "NarrativeText",
                page_number=page_number,
                text=text.strip(),
            )
        )

    notes.append(f"extracted_blocks={len(blocks)}")
    return blocks, notes
