from pathlib import Path

import json

import pytest

from dili_rucam_agents.ingestion.build_bundle import (
    CaseBundleExtractionError,
    CaseBundleExtractionTool,
    build_case_bundle,
)


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
