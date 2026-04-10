from pathlib import Path

import json

import pytest

from dili_rucam_agents.ingestion.build_bundle import (
    CaseBundleExtractionError,
    CaseBundleExtractionTool,
    build_case_bundle,
)
from dili_rucam_agents.ingestion.case_bundle import CaseBundleBlock, CaseBundleTable


FIXTURE_PDF = Path(__file__).resolve().parent / "fixtures" / "example_case.pdf"


def test_build_case_bundle_smoke():
    bundle = build_case_bundle(FIXTURE_PDF)

    assert bundle.pdf_path.endswith("example_case.pdf")
    assert isinstance(bundle.extraction_notes, list)
    assert isinstance(bundle.blocks, list)
    # Even if optional dependencies are missing we still expect a non-null quality section
    assert bundle.quality.unstructured_total_score >= 0


def test_build_case_bundle_missing_file():
    missing_pdf = FIXTURE_PDF.parent / "does_not_exist.pdf"
    with pytest.raises(CaseBundleExtractionError):
        build_case_bundle(missing_pdf)


def test_case_bundle_extraction_tool_outputs_valid_json():
    tool = CaseBundleExtractionTool()
    output = tool._run(str(FIXTURE_PDF))
    payload = json.loads(output)

    assert payload["pdf_path"].endswith("example_case.pdf")
    assert "blocks" in payload and isinstance(payload["blocks"], list)
    assert "tables" in payload and isinstance(payload["tables"], list)


def test_build_case_bundle_uses_docling_fallback_as_last_resort(monkeypatch):
    monkeypatch.setattr(
        "dili_rucam_agents.ingestion.build_bundle.run_unstructured_ingest",
        lambda _: ([], ["unstructured failed"]),
    )
    monkeypatch.setattr(
        "dili_rucam_agents.ingestion.build_bundle.extract_fallback_blocks",
        lambda _: ([], ["pymupdf empty"]),
    )
    monkeypatch.setattr(
        "dili_rucam_agents.ingestion.build_bundle.extract_tables",
        lambda _: ([], ["pdfplumber empty"]),
    )
    monkeypatch.setattr(
        "dili_rucam_agents.ingestion.build_bundle.extract_docling_blocks_and_tables",
        lambda _: (
            [CaseBundleBlock(element_type="DoclingMarkdown", page_number=1, text="Docling extracted text")],
            [CaseBundleTable(page_number=1, table_index=1, raw_rows=[["ALT", "211"]], preview="ALT | 211")],
            ["docling full-page OCR fallback enabled", "docling tables extracted=1"],
        ),
    )

    bundle = build_case_bundle(FIXTURE_PDF)

    assert bundle.normalized_text == "Docling extracted text"
    assert bundle.tables[0].preview == "ALT | 211"
    assert "docling full-page OCR fallback enabled" in bundle.extraction_notes


def test_build_case_bundle_skips_docling_when_primary_extractors_succeed(monkeypatch):
    monkeypatch.setattr(
        "dili_rucam_agents.ingestion.build_bundle.run_unstructured_ingest",
        lambda _: ([CaseBundleBlock(element_type="NarrativeText", page_number=1, text="Primary text")], []),
    )
    monkeypatch.setattr(
        "dili_rucam_agents.ingestion.build_bundle.extract_fallback_blocks",
        lambda _: ([], []),
    )
    monkeypatch.setattr(
        "dili_rucam_agents.ingestion.build_bundle.extract_tables",
        lambda _: ([], []),
    )

    def fail_if_called(_):
        raise AssertionError("docling fallback should not run when primary extraction succeeds")

    monkeypatch.setattr(
        "dili_rucam_agents.ingestion.build_bundle.extract_docling_blocks_and_tables",
        fail_if_called,
    )

    bundle = build_case_bundle(FIXTURE_PDF)

    assert bundle.normalized_text == "Primary text"
