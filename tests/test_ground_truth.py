from dili_rucam_agents.ground_truth import (
    extract_ground_truth_rucam_category,
    extract_ground_truth_rucam_score,
    load_ground_truth_prompt,
)


def test_load_ground_truth_prompt_contains_stable_fields():
    prompt = load_ground_truth_prompt()

    assert "GROUND_TRUTH_RUCAM_SCORE" in prompt
    assert "GROUND_TRUTH_RUCAM_CATEGORY" in prompt
    assert "## Evidence" in prompt


def test_extract_ground_truth_rucam_score_reads_stable_field():
    report = """
# Ground Truth RUCAM Score Report

## Stable Fields
```text
GROUND_TRUTH_RUCAM_SCORE: 8
GROUND_TRUTH_RUCAM_CATEGORY: Probable
```
"""

    assert extract_ground_truth_rucam_score(report) == 8


def test_extract_ground_truth_rucam_category_reads_stable_field():
    report = """
# Ground Truth RUCAM Score Report

## Stable Fields
```text
GROUND_TRUTH_RUCAM_SCORE: 8
GROUND_TRUTH_RUCAM_CATEGORY: Highly probable
```
"""

    assert extract_ground_truth_rucam_category(report) == "Highly probable"
