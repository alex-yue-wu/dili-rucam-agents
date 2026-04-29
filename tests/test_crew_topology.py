from pathlib import Path

from dili_rucam_agents.crew.crew import (
    _prepare_case_bundle_json,
    build_crew,
    run_crew,
)
from dili_rucam_agents.ground_truth import load_ground_truth_prompt
from dili_rucam_agents.crew.tasks import (
    DEFAULT_RUCAM_INFERRING_PROMPT_PATH,
    DEFAULT_RUCAM_STRICT_PROMPT_PATH,
    load_rucam_prompt,
)
from dili_rucam_agents.pipeline import (
    _persist_reports,
    _render_masked_case_bundle_report,
)


def test_build_crew_defaults_to_three_analysts_without_masking():
    crew, task_map = build_crew(pdf_path="dummy.pdf")

    assert [task.name for task in crew.tasks] == [
        "case_bundle_generation",
        "analyst_alpha_analysis",
        "analyst_beta_analysis",
        "analyst_gamma_analysis",
    ]
    assert "analyst_alpha" in task_map
    assert "analyst_beta" in task_map
    assert "analyst_gamma" in task_map
    assert "analyst_delta" not in task_map
    assert "gpt_52" in task_map
    assert "gemini_30" in task_map
    assert "Never quote, restate, compare against, or discuss any author-reported" in crew.tasks[1].description


def test_build_crew_can_enable_masking_and_optional_analysts():
    crew, task_map = build_crew(
        pdf_path="dummy.pdf",
        use_analyst_delta=True,
        use_analyst_epsilon=True,
        use_analyst_zeta=True,
        use_analyst_eta=True,
    )

    assert crew.tasks[1].name == "analyst_alpha_analysis"
    assert "ground_truth_rucam_score" not in task_map
    for key in (
        "analyst_alpha",
        "analyst_beta",
        "analyst_gamma",
        "analyst_delta",
        "analyst_epsilon",
        "analyst_zeta",
        "analyst_eta",
    ):
        context = getattr(task_map[key], "context", [])
        if not isinstance(context, (list, tuple)):
            context = []
        assert all(getattr(task, "name", None) != "case_bundle_generation" for task in context)


def test_prepare_case_bundle_json_masks_deterministically(monkeypatch):
    class DummyBundle:
        def to_dict(self):
            return {
                "pdf_path": "example.pdf",
                "extraction_notes": [],
                "blocks": [{"element_type": "NarrativeText", "page_number": 1, "text": "RUCAM score 8"}],
                "normalized_text": "RUCAM score 8",
                "tables": [],
                "unknowns": [],
                "quality": {"unstructured_total_score": 1, "fallback_pages": [], "fallback_total_score": 0},
            }

    monkeypatch.setattr(
        "dili_rucam_agents.crew.crew.build_case_bundle",
        lambda _: DummyBundle(),
    )

    raw_json, masked_json = _prepare_case_bundle_json(
        pdf_path="dummy.pdf",
        enable_score_masking=True,
    )

    assert '"RUCAM score 8"' in raw_json
    assert masked_json is not None
    assert "RUCAM score [RUCAM_SCORE_MASKED]" in masked_json


def test_run_crew_passes_masked_bundle_to_analysts(monkeypatch):
    captured_inputs = []
    ground_truth_calls = []

    class DummyBundle:
        def to_dict(self):
            return {
                "pdf_path": "example.pdf",
                "extraction_notes": [],
                "blocks": [{"element_type": "NarrativeText", "page_number": 1, "text": "RUCAM score 8"}],
                "normalized_text": "RUCAM score 8",
                "tables": [],
                "unknowns": [],
                "quality": {"unstructured_total_score": 1, "fallback_pages": [], "fallback_total_score": 0},
            }

    class DummyCrew:
        def __init__(self, label):
            self.label = label

        def kickoff(self, inputs):
            captured_inputs.append({"label": self.label, **inputs})
            return f"{self.label} done"

    class DummyTask:
        output = None

    monkeypatch.setattr("dili_rucam_agents.crew.crew.build_case_bundle", lambda _: DummyBundle())
    monkeypatch.setattr(
        "dili_rucam_agents.crew.crew._build_isolated_analyst_runs",
        lambda **kwargs: [
            ("analyst_alpha", DummyCrew("alpha"), DummyTask()),
            ("analyst_beta", DummyCrew("beta"), DummyTask()),
        ],
    )
    monkeypatch.setattr(
        "dili_rucam_agents.crew.crew._run_ground_truth_score_finder",
        lambda raw_json: ground_truth_calls.append(raw_json) or "ground truth report",
    )

    run_crew("dummy.pdf", enable_score_masking=True)

    assert len(captured_inputs) == 2
    for captured in captured_inputs:
        assert "RUCAM score [RUCAM_SCORE_MASKED]" in captured["masked_case_bundle_json"]
        assert "raw_case_bundle_json" not in captured
        assert "prepared_case_bundle_json" not in captured
    assert ground_truth_calls == []


def test_run_crew_captures_ground_truth_report_outside_analyst_crew(monkeypatch):
    captured_inputs = []
    ground_truth_calls = []

    class DummyBundle:
        def to_dict(self):
            return {
                "pdf_path": "example.pdf",
                "extraction_notes": [],
                "blocks": [{"element_type": "NarrativeText", "page_number": 1, "text": "RUCAM score 8"}],
                "normalized_text": "RUCAM score 8",
                "tables": [],
                "unknowns": [],
                "quality": {"unstructured_total_score": 1, "fallback_pages": [], "fallback_total_score": 0},
            }

    class DummyCrew:
        def __init__(self, label):
            self.label = label

        def kickoff(self, inputs):
            captured_inputs.append({"label": self.label, **inputs})
            return f"{self.label} done"

    class DummyTask:
        output = None

    monkeypatch.setattr("dili_rucam_agents.crew.crew.build_case_bundle", lambda _: DummyBundle())
    monkeypatch.setattr(
        "dili_rucam_agents.crew.crew._build_isolated_analyst_runs",
        lambda **kwargs: [("analyst_alpha", DummyCrew("alpha"), DummyTask())],
    )
    monkeypatch.setattr(
        "dili_rucam_agents.crew.crew._run_ground_truth_score_finder",
        lambda raw_json: ground_truth_calls.append(raw_json) or "ground truth report",
    )

    _, reports = run_crew("dummy.pdf", enable_score_masking=True, capture_reports=True)

    assert len(captured_inputs) == 1
    assert "raw_case_bundle_json" not in captured_inputs[0]
    assert len(ground_truth_calls) == 1
    assert "RUCAM score 8" in ground_truth_calls[0]
    assert reports["ground_truth_rucam_score"] == "ground truth report"


def test_run_crew_without_masking_passes_raw_bundle_to_analysts(monkeypatch):
    captured_inputs = []

    class DummyBundle:
        def to_dict(self):
            return {
                "pdf_path": "example.pdf",
                "extraction_notes": [],
                "blocks": [{"element_type": "NarrativeText", "page_number": 1, "text": "RUCAM score 8"}],
                "normalized_text": "RUCAM score 8",
                "tables": [],
                "unknowns": [],
                "quality": {"unstructured_total_score": 1, "fallback_pages": [], "fallback_total_score": 0},
            }

    class DummyCrew:
        def __init__(self, label):
            self.label = label

        def kickoff(self, inputs):
            captured_inputs.append({"label": self.label, **inputs})
            return f"{self.label} done"

    class DummyTask:
        output = None

    monkeypatch.setattr("dili_rucam_agents.crew.crew.build_case_bundle", lambda _: DummyBundle())
    monkeypatch.setattr(
        "dili_rucam_agents.crew.crew._build_isolated_analyst_runs",
        lambda **kwargs: [("analyst_alpha", DummyCrew("alpha"), DummyTask())],
    )

    run_crew("dummy.pdf", enable_score_masking=False)

    assert len(captured_inputs) == 1
    assert "RUCAM score 8" in captured_inputs[0]["raw_case_bundle_json"]
    assert "masked_case_bundle_json" not in captured_inputs[0]
    assert "prepared_case_bundle_json" not in captured_inputs[0]


def test_run_crew_leaves_analyst_reports_untouched_when_masking_enabled(monkeypatch):
    class DummyBundle:
        def to_dict(self):
            return {
                "pdf_path": "example.pdf",
                "extraction_notes": [],
                "blocks": [{"element_type": "NarrativeText", "page_number": 1, "text": "RUCAM score 8"}],
                "normalized_text": "RUCAM score 8",
                "tables": [],
                "unknowns": [],
                "quality": {"unstructured_total_score": 1, "fallback_pages": [], "fallback_total_score": 0},
            }

    class DummyOutput:
        raw = (
            "The authors of the case report calculated a score of 8. "
            "Applying the strict standardized RUCAM rules yields a score of 5."
        )

    class DummyTask:
        output = DummyOutput()

    class DummyCrew:
        def kickoff(self, inputs):
            return (
                "The authors of the case report calculated a score of 8. "
                "Applying the strict standardized RUCAM rules yields a score of 5."
            )

    monkeypatch.setattr("dili_rucam_agents.crew.crew.build_case_bundle", lambda _: DummyBundle())
    monkeypatch.setattr(
        "dili_rucam_agents.crew.crew._build_isolated_analyst_runs",
        lambda **kwargs: [("analyst_alpha", DummyCrew(), DummyTask())],
    )
    monkeypatch.setattr(
        "dili_rucam_agents.crew.crew._run_ground_truth_score_finder",
        lambda raw_json: "ground truth report",
    )

    final_output, reports = run_crew("dummy.pdf", enable_score_masking=True, capture_reports=True)

    assert "calculated a score of 8" in final_output
    assert "yields a score of 5" in final_output
    assert "calculated a score of 8" in reports["analyst_alpha"]
    assert "yields a score of 5" in reports["analyst_alpha"]


def test_persist_reports_writes_analyst_files(tmp_path: Path):
    reports = {
        "analyst_alpha": "alpha",
        "analyst_beta": "beta",
        "analyst_gamma": "gamma",
        "analyst_eta": "eta",
        "ignored": "nope",
    }

    _persist_reports(reports, tmp_path)

    assert (tmp_path / "analyst-alpha_report.md").read_text(encoding="utf-8") == "alpha"
    assert (tmp_path / "analyst-beta_report.md").read_text(encoding="utf-8") == "beta"
    assert (tmp_path / "analyst-gamma_report.md").read_text(encoding="utf-8") == "gamma"
    assert (tmp_path / "analyst-eta_report.md").read_text(encoding="utf-8") == "eta"
    assert not (tmp_path / "ignored_report.md").exists()


def test_persist_reports_writes_masked_case_bundle_markdown(tmp_path: Path):
    reports = {
        "raw_case_bundle": (
            '{"pdf_path":"example.pdf","extraction_notes":["mask applied"],'
            '"blocks":[],"normalized_text":"RUCAM score 8\\nALT 650",'
            '"tables":[{"page_number":1,"table_index":1,"raw_rows":[],"preview":"RUCAM scoring table"}],'
            '"unknowns":[],"quality":{"unstructured_total_score":0,"fallback_pages":[],"fallback_total_score":0}}'
        ),
        "masked_case_bundle": (
            '{"pdf_path":"example.pdf","extraction_notes":["mask applied"],'
            '"blocks":[],"normalized_text":"RUCAM score [RUCAM_SCORE_MASKED]\\nALT 650",'
            '"tables":[{"page_number":1,"table_index":1,"raw_rows":[["RUCAM Item","Score"],["Time to onset","[RUCAM_SCORE_MASKED]"]],"preview":"RUCAM scoring table"}],'
            '"unknowns":[],"quality":{"unstructured_total_score":0,"fallback_pages":[],"fallback_total_score":0}}'
        ),
        "ground_truth_rucam_score": (
            "# Ground Truth RUCAM Score Report\n\n"
            "## Summary\n"
            "- PDF Path: `example.pdf`\n"
            "- Ground Truth Score Found: `yes`\n"
            "- Ground Truth Category Found: `yes`\n\n"
            "## Ground Truth RUCAM Outcome\n"
            "```text\nScore: 8\nCategory: Probable\n```\n\n"
            "## Stable Fields\n"
            "```text\nGROUND_TRUTH_RUCAM_SCORE: 8\nGROUND_TRUTH_RUCAM_CATEGORY: Probable\n```\n\n"
            "## Evidence\n"
            "- Location: `normalized_text line 1`\n"
            "```text\nRUCAM score 8 probable\n```\n"
        )
    }

    _persist_reports(reports, tmp_path)

    content = (tmp_path / "masked-case-bundle_report.md").read_text(encoding="utf-8")
    ground_truth_content = (tmp_path / "ground-truth-rucam-score_report.md").read_text(encoding="utf-8")
    assert "# Masked Case Bundle Report" in content
    assert "## Masked RUCAM Scores" in content
    assert "```text\n8\n```" in content
    assert "MASKED_RUCAM_SCORES: 8" in content
    assert "## Masked Text" in content
    assert "RUCAM score [RUCAM_SCORE_MASKED]" in content
    assert "RUCAM score 8" in content
    assert "No masked table previews found." in content
    assert "# Ground Truth RUCAM Score Report" in ground_truth_content
    assert "## Ground Truth RUCAM Outcome" in ground_truth_content
    assert "Score: 8" in ground_truth_content
    assert "Category: Probable" in ground_truth_content
    assert "GROUND_TRUTH_RUCAM_SCORE: 8" in ground_truth_content
    assert "GROUND_TRUTH_RUCAM_CATEGORY: Probable" in ground_truth_content


def test_persist_reports_does_not_write_masking_review_without_masking(tmp_path: Path):
    reports = {
        "analyst_alpha": "alpha",
        "ground_truth_rucam_score": "should be ignored",
    }

    _persist_reports(reports, tmp_path)

    assert (tmp_path / "analyst-alpha_report.md").read_text(encoding="utf-8") == "alpha"
    assert not (tmp_path / "masked-case-bundle_report.md").exists()
    assert not (tmp_path / "ground-truth-rucam-score_report.md").exists()


def test_render_masked_case_bundle_report_is_consistent_markdown():
    content = _render_masked_case_bundle_report(
        raw_case_bundle_payload='{"pdf_path":"example.pdf","extraction_notes":[],"blocks":[],"normalized_text":"original text","tables":[],"unknowns":[],"quality":{"unstructured_total_score":0,"fallback_pages":[],"fallback_total_score":0}}',
        masked_case_bundle_payload='{"pdf_path":"example.pdf","extraction_notes":[],"blocks":[],"normalized_text":"masked text","tables":[],"unknowns":[],"quality":{"unstructured_total_score":0,"fallback_pages":[],"fallback_total_score":0}}',
    )

    assert content.startswith("# Masked Case Bundle Report\n")
    assert "## Summary" in content
    assert "## Stable Fields" in content
    assert "## Extraction Notes" in content
    assert "## Masked Table Previews" in content
    assert "No masked text found." in content


def test_render_masked_case_bundle_report_extracts_scores_from_masked_table_rows():
    content = _render_masked_case_bundle_report(
        raw_case_bundle_payload=(
            '{"pdf_path":"example.pdf","extraction_notes":[],"blocks":[],"normalized_text":"",'
            '"tables":[{"page_number":1,"table_index":1,"raw_rows":[["RUCAM Item","Score"],["Time to onset","+2"],["Course","-1"]],"preview":"RUCAM scoring table"}],'
            '"unknowns":[],"quality":{"unstructured_total_score":0,"fallback_pages":[],"fallback_total_score":0}}'
        ),
        masked_case_bundle_payload=(
            '{"pdf_path":"example.pdf","extraction_notes":[],"blocks":[],"normalized_text":"",'
            '"tables":[{"page_number":1,"table_index":1,"raw_rows":[["RUCAM Item","Score"],["Time to onset","[RUCAM_SCORE_MASKED]"],["Course","[RUCAM_SCORE_MASKED]"]],"preview":"RUCAM scoring table"}],'
            '"unknowns":[],"quality":{"unstructured_total_score":0,"fallback_pages":[],"fallback_total_score":0}}'
        ),
    )

    assert "## Masked RUCAM Scores" in content
    assert "```text\n\n```" in content
    assert "MASKED_RUCAM_SCORES: None" in content


def test_render_masked_case_bundle_report_extracts_only_patient_specific_outcome_scores():
    content = _render_masked_case_bundle_report(
        raw_case_bundle_payload=(
            '{"pdf_path":"example.pdf","extraction_notes":[],"blocks":[],"normalized_text":"'
            'When it is applied, a score from -10 to +14 is obtained and is used to classify causality into five categories.\\n'
            'The Roussel UCLAF method was applied for an acute hepatocellular problem, with a final score of 8.",'
            '"tables":[],"unknowns":[],"quality":{"unstructured_total_score":0,"fallback_pages":[],"fallback_total_score":0}}'
        ),
        masked_case_bundle_payload=(
            '{"pdf_path":"example.pdf","extraction_notes":[],"blocks":[],"normalized_text":"'
            'When it is applied, a score from -10 to +14 is obtained and is used to classify causality into five categories.\\n'
            'The Roussel UCLAF method was applied for an acute hepatocellular problem, with a final score of [RUCAM_SCORE_MASKED].",'
            '"tables":[],"unknowns":[],"quality":{"unstructured_total_score":0,"fallback_pages":[],"fallback_total_score":0}}'
        ),
    )

    assert "```text\n8\n```" in content
    assert "MASKED_RUCAM_SCORES: 8" in content


def test_ground_truth_prompt_requests_single_score_and_category():
    prompt = load_ground_truth_prompt()

    assert "GROUND_TRUTH_RUCAM_SCORE" in prompt
    assert "GROUND_TRUTH_RUCAM_CATEGORY" in prompt
    assert "Output exactly one score and one category." in prompt


def test_load_rucam_prompt_defaults_to_inferring_mode():
    prompt = load_rucam_prompt(strict_scoring=False)

    assert prompt == load_rucam_prompt()
    assert prompt == DEFAULT_RUCAM_INFERRING_PROMPT_PATH.read_text(encoding="utf-8")


def test_load_rucam_prompt_uses_strict_mode_when_requested():
    prompt = load_rucam_prompt(strict_scoring=True)

    assert prompt == DEFAULT_RUCAM_STRICT_PROMPT_PATH.read_text(encoding="utf-8")
