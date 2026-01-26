from dili_rucam_agents.ingestion.case_bundle import (
    CaseBundle,
    CaseBundleBlock,
    CaseBundleTable,
    QualityMetrics,
    merge_blocks,
)


def test_case_bundle_to_dict_structure():
    bundle = CaseBundle(
        pdf_path="dummy.pdf",
        extraction_notes=["note"],
        blocks=[CaseBundleBlock(element_type="NarrativeText", page_number=1, text="Example")],
        normalized_text="Example",
        tables=[
            CaseBundleTable(page_number=1, table_index=1, raw_rows=[["col1", "col2"]], preview="col1 | col2")
        ],
        unknowns=[],
        quality=QualityMetrics(unstructured_total_score=1),
    )

    bundle_dict = bundle.to_dict()

    assert bundle_dict["pdf_path"] == "dummy.pdf"
    assert bundle_dict["blocks"][0]["element_type"] == "NarrativeText"
    assert bundle_dict["tables"][0]["table_index"] == 1
    assert bundle_dict["quality"]["unstructured_total_score"] == 1


def test_merge_blocks_removes_duplicates_and_sorts():
    blocks_a = [
        CaseBundleBlock(element_type="Title", page_number=1, text="Case Report"),
        CaseBundleBlock(element_type="NarrativeText", page_number=2, text="Details A"),
    ]
    blocks_b = [
        CaseBundleBlock(element_type="NarrativeText", page_number=2, text="Details A"),
        CaseBundleBlock(element_type="Caption", page_number=3, text="Figure 1"),
    ]

    merged = merge_blocks(blocks_a, blocks_b)

    assert len(merged) == 3
    # Sorted order should be by page_number then element_type
    assert merged[0].element_type == "Title"
    assert merged[1].element_type == "NarrativeText"
    assert merged[2].element_type == "Caption"
