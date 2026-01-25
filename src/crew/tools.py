from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

from crewai.tools import BaseTool
from pypdf import PdfReader


@dataclass
class CaseBundleBlock:
    element_type: str
    page_number: int
    text: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "element_type": self.element_type,
            "page_number": self.page_number,
            "text": self.text,
        }


@dataclass
class CaseBundle:
    pdf_path: str
    extraction_notes: List[str]
    blocks: List[CaseBundleBlock]
    normalized_text: str
    tables: List[Dict[str, Any]]
    unknowns: List[str]
    quality: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pdf_path": self.pdf_path,
            "extraction_notes": self.extraction_notes,
            "blocks": [block.to_dict() for block in self.blocks],
            "normalized_text": self.normalized_text,
            "tables": self.tables,
            "unknowns": self.unknowns,
            "quality": self.quality,
        }


class CaseBundleExtractionTool(BaseTool):
    """Deterministic PDF ingestion utility that emits canonical case_bundle JSON."""

    name: str = "case_bundle_extractor"
    description: str = (
        "Use this tool to convert a clinical PDF into the canonical case_bundle_json "
        "object required by the RUCAM analysts. Provide an absolute or relative path "
        "to the PDF you want to ingest."
    )

    def _run(self, pdf_path: str) -> str:
        case_bundle = self._extract_case_bundle(Path(pdf_path))
        return json.dumps(case_bundle.to_dict(), indent=2)

    async def _arun(self, pdf_path: str) -> str:  # pragma: no cover - async parity
        return self._run(pdf_path)

    def _extract_case_bundle(self, pdf_path: Path) -> CaseBundle:
        if not pdf_path.exists():
            raise FileNotFoundError(f"PDF not found: {pdf_path}")

        reader = PdfReader(str(pdf_path))
        blocks: List[CaseBundleBlock] = []
        tables: List[Dict[str, Any]] = []
        normalized_lines: List[str] = []

        for page_number, page in enumerate(reader.pages, start=1):
            raw_text = page.extract_text() or ""
            page_lines = [line.strip() for line in raw_text.splitlines() if line.strip()]

            if not page_lines:
                blocks.append(
                    CaseBundleBlock(
                        element_type="Unknown",
                        page_number=page_number,
                        text="",
                    )
                )
                continue

            paragraph = ""
            for line in page_lines:
                normalized_lines.append(line)
                if self._looks_like_table(line):
                    if paragraph:
                        blocks.append(
                            CaseBundleBlock(
                                element_type="NarrativeText",
                                page_number=page_number,
                                text=paragraph.strip(),
                            )
                        )
                        paragraph = ""

                    table_rows = self._table_rows(page_lines)
                    if table_rows:
                        tables.append(
                            {
                                "page_number": page_number,
                                "table_index": len(tables) + 1,
                                "raw_rows": table_rows,
                                "preview": "\n".join(" | ".join(row) for row in table_rows[:3]),
                            }
                        )
                    break

                paragraph = f"{paragraph} {line}".strip()

            if paragraph:
                blocks.append(
                    CaseBundleBlock(
                        element_type="NarrativeText",
                        page_number=page_number,
                        text=paragraph,
                    )
                )

        extraction_notes = [
            "Deterministic ingestion completed with PyPDF text extraction.",
            "Tables are approximated using delimiter heuristics.",
        ]

        quality = {
            "unstructured_total_score": 0,
            "fallback_pages": [],
            "fallback_total_score": 0,
        }

        return CaseBundle(
            pdf_path=str(pdf_path),
            extraction_notes=extraction_notes,
            blocks=blocks,
            normalized_text="\n".join(normalized_lines),
            tables=tables,
            unknowns=[],
            quality=quality,
        )

    def _looks_like_table(self, line: str) -> bool:
        delimiters = ["\t", "|", ","]
        return any(delim in line for delim in delimiters)

    def _table_rows(self, lines: List[str]) -> List[List[str]]:
        rows: List[List[str]] = []
        for line in lines:
            if not self._looks_like_table(line):
                continue
            delimiter = "\t"
            for candidate in ["|", ",", "\t"]:
                if candidate in line:
                    delimiter = candidate
                    break
            rows.append([cell.strip() for cell in line.split(delimiter) if cell.strip()])
        return rows
