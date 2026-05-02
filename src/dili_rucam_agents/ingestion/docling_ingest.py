from __future__ import annotations

import re
from pathlib import Path
from typing import List, Tuple

from .case_bundle import CaseBundleBlock, CaseBundleTable


def extract_docling_blocks_and_tables(
    pdf_path: Path,
) -> Tuple[List[CaseBundleBlock], List[CaseBundleTable], List[str]]:
    """Best-effort Docling full-page OCR fallback for text and tables."""

    try:
        from docling.datamodel.base_models import InputFormat  # type: ignore
        from docling.datamodel.pipeline_options import (  # type: ignore
            PdfPipelineOptions,
            TableStructureOptions,
            TesseractCliOcrOptions,
        )
        from docling.document_converter import DocumentConverter, PdfFormatOption  # type: ignore
    except Exception as exc:  # pragma: no cover - dependency optional
        return [], [], [f"docling fallback unavailable: {exc}"]

    notes = [
        "docling full-page OCR fallback enabled",
        "docling risk note: validate representative multi-column PDFs before adopting broadly",
    ]

    try:
        pipeline_options = PdfPipelineOptions()
        pipeline_options.do_ocr = True
        pipeline_options.do_table_structure = True
        pipeline_options.table_structure_options = TableStructureOptions(
            do_cell_matching=True
        )
        pipeline_options.ocr_options = TesseractCliOcrOptions(force_full_page_ocr=True)

        converter = DocumentConverter(
            format_options={
                InputFormat.PDF: PdfFormatOption(
                    pipeline_options=pipeline_options,
                )
            }
        )
        document = converter.convert(pdf_path).document
        markdown = document.export_to_markdown()
    except Exception as exc:  # pragma: no cover - environment dependent
        notes.append(f"docling fallback failed: {exc}")
        return [], [], notes

    if not markdown.strip():
        notes.append("docling markdown empty")
        return [], [], notes

    blocks = [
        CaseBundleBlock(
            element_type="DoclingMarkdown",
            page_number=1,
            text=markdown.strip(),
        )
    ]
    tables = _extract_markdown_tables(markdown)
    notes.append(f"docling markdown chars={len(markdown.strip())}")
    notes.append(f"docling tables extracted={len(tables)}")
    return blocks, tables, notes


def _extract_markdown_tables(markdown: str) -> List[CaseBundleTable]:
    lines = markdown.splitlines()
    groups: List[list[str]] = []
    current: list[str] = []

    for line in lines:
        if "|" in line and line.strip():
            current.append(line.rstrip())
            continue
        if current:
            groups.append(current)
            current = []
    if current:
        groups.append(current)

    tables: List[CaseBundleTable] = []
    for group in groups:
        rows = _parse_markdown_table(group)
        if not rows:
            continue
        tables.append(
            CaseBundleTable(
                page_number=1,
                table_index=len(tables) + 1,
                raw_rows=rows,
                preview=" | ".join(rows[0]),
            )
        )
    return tables


def _parse_markdown_table(lines: list[str]) -> List[List[str]]:
    rows: List[List[str]] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if _is_markdown_separator_row(stripped):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if not any(cells):
            continue
        rows.append(cells)
    return rows


def _is_markdown_separator_row(line: str) -> bool:
    cells = [cell.strip() for cell in line.strip("|").split("|")]
    if not cells:
        return False
    return all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)


__all__ = ["extract_docling_blocks_and_tables"]
