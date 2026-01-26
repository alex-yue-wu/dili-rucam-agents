from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence


@dataclass
class CaseBundleBlock:
    """Normalized narrative/text block coming from any extractor."""

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
class CaseBundleTable:
    page_number: int
    table_index: int
    raw_rows: List[List[str]]
    preview: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "page_number": self.page_number,
            "table_index": self.table_index,
            "raw_rows": self.raw_rows,
            "preview": self.preview,
        }


@dataclass
class QualityMetrics:
    unstructured_total_score: int = 0
    fallback_pages: List[int] = field(default_factory=list)
    fallback_total_score: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "unstructured_total_score": self.unstructured_total_score,
            "fallback_pages": self.fallback_pages,
            "fallback_total_score": self.fallback_total_score,
        }


@dataclass
class CaseBundle:
    pdf_path: str
    extraction_notes: List[str]
    blocks: List[CaseBundleBlock]
    normalized_text: str
    tables: List[CaseBundleTable]
    unknowns: List[str]
    quality: QualityMetrics

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pdf_path": self.pdf_path,
            "extraction_notes": self.extraction_notes,
            "blocks": [block.to_dict() for block in self.blocks],
            "normalized_text": self.normalized_text,
            "tables": [table.to_dict() for table in self.tables],
            "unknowns": self.unknowns,
            "quality": self.quality.to_dict(),
        }


def merge_blocks(*block_sequences: Sequence[CaseBundleBlock]) -> List[CaseBundleBlock]:
    blocks: List[CaseBundleBlock] = []
    seen: set[tuple[int, str]] = set()
    for seq in block_sequences:
        for block in seq:
            key = (block.page_number, block.text)
            if key in seen:
                continue
            seen.add(key)
            blocks.append(block)
    return sorted(blocks, key=lambda b: (b.page_number, b.element_type))
