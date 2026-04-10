from dili_rucam_agents.masking import (
    MASK_TOKEN,
    ScoreMaskingTool,
    extract_patient_specific_rucam_scores,
    extract_rucam_grade_labels,
    extract_rucam_score_ints,
    is_patient_specific_outcome_line,
    mask_case_bundle_payload,
    review_masked_case_bundle,
)


def test_mask_case_bundle_payload_redacts_rucam_scores_and_categories():
    payload = {
        "pdf_path": "example.pdf",
        "extraction_notes": [],
        "blocks": [
            {"element_type": "NarrativeText", "page_number": 1, "text": "The RUCAM score was 8 points."},
            {"element_type": "NarrativeText", "page_number": 1, "text": "ALT peaked at 650 U/L."},
        ],
        "normalized_text": "Final RUCAM score and causality category: probable.\nALT peaked at 650 U/L.",
        "tables": [
            {
                "page_number": 1,
                "table_index": 1,
                "raw_rows": [["RUCAM Item", "Score", "Category"], ["Time to onset", "+2", "Highly probable"]],
                "preview": "RUCAM scoring table",
            }
        ],
        "unknowns": [],
        "quality": {"unstructured_total_score": 1, "fallback_pages": [], "fallback_total_score": 0},
    }

    masked = mask_case_bundle_payload(payload)

    assert masked["blocks"][0]["text"] == f"The RUCAM score was {MASK_TOKEN} points."
    assert masked["blocks"][1]["text"] == "ALT peaked at 650 U/L."
    assert masked["normalized_text"].splitlines()[0] == f"Final RUCAM score and causality category: {MASK_TOKEN}."
    assert masked["tables"][0]["preview"] == "RUCAM scoring table"
    assert masked["tables"][0]["raw_rows"][0] == ["RUCAM Item", "Score", "Category"]
    assert masked["tables"][0]["raw_rows"][1] == ["Time to onset", MASK_TOKEN, MASK_TOKEN]
    assert masked["extraction_notes"][-1].startswith("RUCAM score and grade masking applied")


def test_mask_case_bundle_payload_preserves_clinical_presentation_text():
    payload = {
        "pdf_path": "example.pdf",
        "extraction_notes": [],
        "blocks": [
            {
                "element_type": "NarrativeText",
                "page_number": 1,
                "text": "Clinical case presentation: probable jaundice began 3 days after admission.",
            },
            {
                "element_type": "NarrativeText",
                "page_number": 1,
                "text": "RUCAM score was 6 points with probable causality.",
            },
        ],
        "normalized_text": (
            "Clinical case presentation: probable jaundice began 3 days after admission.\n"
            "RUCAM score was 6 points with probable causality."
        ),
        "tables": [],
        "unknowns": [],
        "quality": {"unstructured_total_score": 1, "fallback_pages": [], "fallback_total_score": 0},
    }

    masked = mask_case_bundle_payload(payload)

    assert masked["blocks"][0]["text"] == payload["blocks"][0]["text"]
    assert masked["blocks"][1]["text"] == f"RUCAM score was {MASK_TOKEN} points with {MASK_TOKEN} causality."
    assert masked["normalized_text"].splitlines()[0] == payload["blocks"][0]["text"]
    assert masked["normalized_text"].splitlines()[1] == f"RUCAM score was {MASK_TOKEN} points with {MASK_TOKEN} causality."


def test_mask_case_bundle_payload_masks_author_reported_score_when_rucam_context_appears_later():
    payload = {
        "pdf_path": "example.pdf",
        "extraction_notes": [],
        "blocks": [
            {
                "element_type": "NarrativeText",
                "page_number": 1,
                "text": (
                    "The authors of the case report calculated a score of 8. "
                    "However, applying the strict standardized RUCAM rules yields a score of 5."
                ),
            }
        ],
        "normalized_text": (
            "The authors of the case report calculated a score of 8. "
            "However, applying the strict standardized RUCAM rules yields a score of 5."
        ),
        "tables": [],
        "unknowns": [],
        "quality": {"unstructured_total_score": 1, "fallback_pages": [], "fallback_total_score": 0},
    }

    masked = mask_case_bundle_payload(payload)

    assert "score of [RUCAM_SCORE_MASKED]" in masked["blocks"][0]["text"]
    assert "strict standardized RUCAM rules yields a score of [RUCAM_SCORE_MASKED]" in masked["blocks"][0]["text"]


def test_mask_case_bundle_payload_masks_roussel_uclaf_alias_abstract_score():
    payload = {
        "pdf_path": "example.pdf",
        "extraction_notes": [],
        "blocks": [
            {
                "element_type": "NarrativeText",
                "page_number": 1,
                "text": (
                    "The Roussel UCLAF method for estimating causality of the adverse event was applied "
                    "for an acute hepatocellular problem, with a final score of 8."
                ),
            }
        ],
        "normalized_text": (
            "The Roussel UCLAF method for estimating causality of the adverse event was applied "
            "for an acute hepatocellular problem, with a final score of 8."
        ),
        "tables": [],
        "unknowns": [],
        "quality": {"unstructured_total_score": 1, "fallback_pages": [], "fallback_total_score": 0},
    }

    masked = mask_case_bundle_payload(payload)

    assert "final score of [RUCAM_SCORE_MASKED]" in masked["blocks"][0]["text"]
    assert "final score of [RUCAM_SCORE_MASKED]" in masked["normalized_text"]


def test_mask_case_bundle_payload_does_not_mask_generic_rucam_scale_description():
    payload = {
        "pdf_path": "example.pdf",
        "extraction_notes": [],
        "blocks": [
            {
                "element_type": "NarrativeText",
                "page_number": 1,
                "text": (
                    "When it is applied, a score from -10 to +14 is obtained and is used to classify "
                    "causality into five categories: excluded, improbable, possible, probable, and highly probable."
                ),
            }
        ],
        "normalized_text": (
            "When it is applied, a score from -10 to +14 is obtained and is used to classify "
            "causality into five categories: excluded, improbable, possible, probable, and highly probable."
        ),
        "tables": [],
        "unknowns": [],
        "quality": {"unstructured_total_score": 1, "fallback_pages": [], "fallback_total_score": 0},
    }

    masked = mask_case_bundle_payload(payload)

    assert masked["blocks"][0]["text"] == payload["blocks"][0]["text"]
    assert masked["normalized_text"] == payload["normalized_text"]


def test_mask_case_bundle_payload_masks_only_score_tokens_in_rucam_table():
    payload = {
        "pdf_path": "example.pdf",
        "extraction_notes": [],
        "blocks": [],
        "normalized_text": "",
        "tables": [
            {
                "page_number": 1,
                "table_index": 1,
                "raw_rows": [["RUCAM Item", "Score"], ["Time to onset", "+2"], ["Course", "-1"]],
                "preview": "RUCAM scoring table",
            }
        ],
        "unknowns": [],
        "quality": {"unstructured_total_score": 1, "fallback_pages": [], "fallback_total_score": 0},
    }

    masked = mask_case_bundle_payload(payload)

    assert masked["tables"][0]["raw_rows"] == [
        ["RUCAM Item", "Score"],
        ["Time to onset", MASK_TOKEN],
        ["Course", MASK_TOKEN],
    ]


def test_mask_case_bundle_payload_masks_only_rucam_grade_tokens_in_grade_columns():
    payload = {
        "pdf_path": "example.pdf",
        "extraction_notes": [],
        "blocks": [],
        "normalized_text": "RUCAM causality grade: Highly probable.",
        "tables": [
            {
                "page_number": 1,
                "table_index": 1,
                "raw_rows": [["RUCAM Item", "Category"], ["Overall causality", "Probable"]],
                "preview": "RUCAM scoring table",
            }
        ],
        "unknowns": [],
        "quality": {"unstructured_total_score": 1, "fallback_pages": [], "fallback_total_score": 0},
    }

    masked = mask_case_bundle_payload(payload)

    assert masked["normalized_text"] == f"RUCAM causality grade: {MASK_TOKEN}."
    assert masked["tables"][0]["raw_rows"] == [
        ["RUCAM Item", "Category"],
        ["Overall causality", MASK_TOKEN],
    ]


def test_mask_case_bundle_payload_does_not_mask_lab_table_values():
    payload = {
        "pdf_path": "example.pdf",
        "extraction_notes": [],
        "blocks": [],
        "normalized_text": "",
        "tables": [
            {
                "page_number": 1,
                "table_index": 1,
                "raw_rows": [
                    ["Date", "AST", "ALT", "Alkaline Phosphatase"],
                    ["January 13", "550", "570", "1332"],
                    ["January 24", "289", "227", "646"],
                ],
                "preview": "Hepatic Laboratory Tests",
            }
        ],
        "unknowns": [],
        "quality": {"unstructured_total_score": 1, "fallback_pages": [], "fallback_total_score": 0},
    }

    masked = mask_case_bundle_payload(payload)

    assert masked["tables"][0]["raw_rows"] == payload["tables"][0]["raw_rows"]


def test_mask_case_bundle_payload_does_not_mask_years_or_history_numbers_in_non_rucam_text():
    payload = {
        "pdf_path": "example.pdf",
        "extraction_notes": [],
        "blocks": [
            {
                "element_type": "NarrativeText",
                "page_number": 1,
                "text": "Case report published in 2021 issue 5; symptoms began 3 days after admission.",
            },
            {
                "element_type": "NarrativeText",
                "page_number": 1,
                "text": "RUCAM was discussed in the 2021 review article, but no score was given.",
            },
        ],
        "normalized_text": (
            "Case report published in 2021 issue 5; symptoms began 3 days after admission.\n"
            "RUCAM was discussed in the 2021 review article, but no score was given."
        ),
        "tables": [],
        "unknowns": [],
        "quality": {"unstructured_total_score": 1, "fallback_pages": [], "fallback_total_score": 0},
    }

    masked = mask_case_bundle_payload(payload)

    assert masked["blocks"][0]["text"] == payload["blocks"][0]["text"]
    assert masked["blocks"][1]["text"] == payload["blocks"][1]["text"]
    assert masked["normalized_text"] == payload["normalized_text"]


def test_extract_rucam_score_ints_only_reads_explicit_rucam_score_content():
    assert extract_rucam_score_ints("Clinical presentation: jaundice for 3 days.") == []
    assert extract_rucam_score_ints("RUCAM score was 6 points.") == [6]
    assert extract_rucam_score_ints("RUCAM Item | Score | Time to onset | +2") == [2]


def test_patient_specific_outcome_detection_prefers_true_case_outcomes():
    assert is_patient_specific_outcome_line(
        "The Roussel UCLAF method was applied for an acute hepatocellular problem, with a final score of 8."
    )
    assert extract_patient_specific_rucam_scores(
        "The Roussel UCLAF method was applied for an acute hepatocellular problem, with a final score of 8."
    ) == [8]
    assert not is_patient_specific_outcome_line(
        "When it is applied, a score from -10 to +14 is obtained and used to classify causality."
    )
    assert extract_patient_specific_rucam_scores(
        "When it is applied, a score from -10 to +14 is obtained and used to classify causality."
    ) == []


def test_extract_rucam_grade_labels_only_reads_explicit_rucam_grade_content():
    assert extract_rucam_grade_labels("Clinical presentation: probable jaundice.") == []
    assert extract_rucam_grade_labels("RUCAM causality category: Highly probable.") == ["Highly probable"]


def test_review_masked_case_bundle_flags_likely_unmasked_mentions():
    raw_payload = {
        "pdf_path": "example.pdf",
        "extraction_notes": [],
        "blocks": [],
        "normalized_text": "RUCAM score was 8 with probable causality.",
        "tables": [],
        "unknowns": [],
        "quality": {"unstructured_total_score": 0, "fallback_pages": [], "fallback_total_score": 0},
    }
    masked_payload = {
        "pdf_path": "example.pdf",
        "extraction_notes": [],
        "blocks": [],
        "normalized_text": "RUCAM score was 8 with probable causality.",
        "tables": [],
        "unknowns": [],
        "quality": {"unstructured_total_score": 0, "fallback_pages": [], "fallback_total_score": 0},
    }

    findings = review_masked_case_bundle(raw_payload, masked_payload)

    assert len(findings) == 1
    assert findings[0]["likely_missed_scores"] == [8]
    assert findings[0]["likely_missed_grades"] == ["Probable"]


def test_score_masking_tool_returns_json():
    tool = ScoreMaskingTool()
    output = tool._run(
        '{"pdf_path":"example.pdf","extraction_notes":[],"blocks":[],"normalized_text":"RUCAM score 9","tables":[],"unknowns":[],"quality":{"unstructured_total_score":0,"fallback_pages":[],"fallback_total_score":0}}'
    )

    assert MASK_TOKEN in output


def test_score_masking_tool_accepts_wrapped_json_with_extra_text():
    tool = ScoreMaskingTool()
    output = tool._run(
        'Here is the payload:\n{"pdf_path":"example.pdf","extraction_notes":[],"blocks":[],"normalized_text":"RUCAM score 9","tables":[],"unknowns":[],"quality":{"unstructured_total_score":0,"fallback_pages":[],"fallback_total_score":0}}\nTrailing note'
    )

    assert MASK_TOKEN in output
