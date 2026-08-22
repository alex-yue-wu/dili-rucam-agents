import json
from pathlib import Path

import pytest

from dili_rucam_agents.checkpoints import AnalystCheckpointStore, AnalystIdentity
from dili_rucam_agents.crew.crew import (
    AnalystExecutionError,
    _prepare_case_bundle_json,
    build_crew,
    run_crew,
    validate_max_restarts,
)
from dili_rucam_agents.ground_truth import load_ground_truth_prompt
from dili_rucam_agents.crew.tasks import (
    DEFAULT_RUCAM_INFERRING_PROMPT_PATH,
    DEFAULT_RUCAM_STRICT_PROMPT_PATH,
    build_analyst_instruction_contract,
    load_rucam_prompt,
)
from dili_rucam_agents.pipeline import (
    _persist_reports,
    _render_masked_case_bundle_report,
)


def complete_report(total_score: int = 6, narrative: str = "Clinical summary") -> str:
    payload = {
        "injury_pattern": "hepatocellular",
        "R_ratio": 6.4,
        "rucam_scores": {
            "time_to_onset": 2,
            "course": 1,
            "risk_factors": 0,
            "concomitant_drugs": 0,
            "other_causes_excluded": 2,
            "known_hepatotoxicity": 1,
            "rechallenge": 0,
        },
        "total_score": total_score,
        "category": "Probable",
    }
    return (
        f"## SECTION A\n\n{narrative}\n\n"
        "## SECTION B\n\n| Item | Score |\n| --- | --- |\n| Total | 6 |\n\n"
        f"## SECTION C\n\n```json\n{json.dumps(payload)}\n```\n"
    )


def install_retry_scenario(monkeypatch, outputs_by_key):
    constructed_keys = []

    class DummyBundle:
        def to_dict(self):
            return {
                "pdf_path": "dummy.pdf",
                "extraction_notes": [],
                "blocks": [],
                "normalized_text": "ALT 650",
                "tables": [],
                "unknowns": [],
                "quality": {
                    "unstructured_total_score": 1,
                    "fallback_pages": [],
                    "fallback_total_score": 0,
                },
            }

    class DummyTask:
        output = None

    class DummyCrew:
        def __init__(self, result):
            self.result = result

        def kickoff(self, inputs):
            if isinstance(self.result, Exception):
                raise self.result
            return self.result

    configs = [
        {"key": key, "label": key.replace("_", " ").title()} for key in outputs_by_key
    ]

    def fake_build_run(*, config, **kwargs):
        constructed_keys.append(config["key"])
        return DummyCrew(next(outputs_by_key[config["key"]])), DummyTask()

    monkeypatch.setattr(
        "dili_rucam_agents.crew.crew.build_case_bundle", lambda path: DummyBundle()
    )
    monkeypatch.setattr(
        "dili_rucam_agents.crew.crew.get_enabled_analyst_configs",
        lambda **flags: configs,
    )
    monkeypatch.setattr(
        "dili_rucam_agents.crew.crew._build_isolated_analyst_run", fake_build_run
    )
    return constructed_keys


def test_validate_max_restarts_rejects_non_integer_values():
    with pytest.raises(ValueError, match="integer from 0 through 2"):
        validate_max_restarts(1.5)


def test_run_crew_restarts_only_invalid_analyst_until_third_attempt(monkeypatch):
    outputs_by_key = {
        "analyst_alpha": iter(["incomplete", "still incomplete", complete_report()])
    }
    constructed_keys = install_retry_scenario(monkeypatch, outputs_by_key)
    events = []

    _, reports = run_crew(
        "dummy.pdf", capture_reports=True, max_restarts=2, on_attempt=events.append
    )

    assert constructed_keys == ["analyst_alpha"] * 3
    assert reports["analyst_alpha"].startswith("## SECTION A")
    assert [event.status for event in events] == [
        "running",
        "validation_failed",
        "running",
        "validation_failed",
        "running",
        "completed",
    ]


def test_run_crew_raises_after_three_invalid_attempts(monkeypatch):
    constructed_keys = install_retry_scenario(
        monkeypatch,
        {"analyst_alpha": iter(["incomplete", "incomplete", "incomplete"])},
    )

    with pytest.raises(AnalystExecutionError) as exc_info:
        run_crew("dummy.pdf", capture_reports=True, max_restarts=2)

    assert constructed_keys == ["analyst_alpha"] * 3
    assert exc_info.value.analyst_key == "analyst_alpha"
    assert exc_info.value.attempts == 3
    assert exc_info.value.failure_kind == "validation"


def test_run_crew_skips_completed_alpha_and_retries_beta(monkeypatch):
    constructed_keys = install_retry_scenario(
        monkeypatch,
        {
            "analyst_alpha": iter(()),
            "analyst_beta": iter(["incomplete", complete_report(narrative="beta")]),
            "analyst_gamma": iter([complete_report(narrative="gamma")]),
        },
    )

    _, reports = run_crew(
        "dummy.pdf",
        capture_reports=True,
        completed_reports={"analyst_alpha": complete_report(narrative="saved alpha")},
        max_restarts=2,
    )

    assert constructed_keys == ["analyst_beta", "analyst_beta", "analyst_gamma"]
    assert "saved alpha" in reports["analyst_alpha"]


def test_run_crew_does_not_skip_invalid_supplied_completed_report(monkeypatch):
    replacement = complete_report(narrative="fresh alpha")
    constructed_keys = install_retry_scenario(
        monkeypatch,
        {"analyst_alpha": iter([replacement])},
    )

    _, reports = run_crew(
        "dummy.pdf",
        capture_reports=True,
        completed_reports={"analyst_alpha": "unvalidated checkpoint text"},
        max_restarts=0,
    )

    assert constructed_keys == ["analyst_alpha"]
    assert reports == {"analyst_alpha": replacement}


def test_run_crew_restarts_after_execution_exception(monkeypatch):
    install_retry_scenario(
        monkeypatch,
        {
            "analyst_alpha": iter(
                [RuntimeError("provider unavailable"), complete_report()]
            )
        },
    )
    events = []

    run_crew("dummy.pdf", capture_reports=True, on_attempt=events.append)

    assert [event.status for event in events] == [
        "running",
        "execution_failed",
        "running",
        "completed",
    ]


def test_validation_secrets_do_not_reach_retry_events_or_manifest(
    tmp_path, monkeypatch
):
    secret = "MODEL-CONTROLLED-SECRET"
    invalid_report = complete_report().replace(
        '"time_to_onset": 2', f'"time_to_onset": "{secret}"'
    )
    outputs = iter((invalid_report, complete_report()))
    retry_instructions = []

    class DummyBundle:
        def to_dict(self):
            return {
                "pdf_path": "dummy.pdf",
                "extraction_notes": [],
                "blocks": [],
                "normalized_text": "ALT 650",
                "tables": [],
                "unknowns": [],
                "quality": {
                    "unstructured_total_score": 1,
                    "fallback_pages": [],
                    "fallback_total_score": 0,
                },
            }

    class DummyTask:
        output = None

    class DummyCrew:
        def __init__(self, report_text):
            self.report_text = report_text

        def kickoff(self, inputs):
            return self.report_text

    monkeypatch.setattr(
        "dili_rucam_agents.crew.crew.build_case_bundle", lambda path: DummyBundle()
    )
    monkeypatch.setattr(
        "dili_rucam_agents.crew.crew.get_enabled_analyst_configs",
        lambda **flags: [{"key": "analyst_alpha", "label": "Analyst Alpha"}],
    )

    def fake_build_run(*, retry_instruction, **kwargs):
        retry_instructions.append(retry_instruction)
        return DummyCrew(next(outputs)), DummyTask()

    monkeypatch.setattr(
        "dili_rucam_agents.crew.crew._build_isolated_analyst_run", fake_build_run
    )
    store = AnalystCheckpointStore(
        tmp_path,
        pdf_filename="case.pdf",
        pdf_sha256="pdf",
        enabled_analysts=("analyst_alpha",),
    )
    identity = AnalystIdentity(
        key="analyst_alpha",
        report_filename="analyst-alpha_report.md",
        fingerprint="fingerprint-a",
    )
    events = []

    def record_event(event):
        events.append(event)
        if event.status == "running":
            store.record_running(identity, attempt=event.attempt)
        elif event.status == "validation_failed":
            store.record_failure(
                identity,
                attempt=event.attempt,
                failure_kind="validation",
                error=event.error,
                report_text=event.report_text,
            )

    run_crew(
        "dummy.pdf",
        capture_reports=True,
        max_restarts=1,
        on_attempt=record_event,
    )

    validation_event = next(
        event for event in events if event.status == "validation_failed"
    )
    manifest_text = (tmp_path / "analyst_checkpoints.json").read_text()
    assert retry_instructions[0] is None
    assert "rucam_scores.time_to_onset" in retry_instructions[1]
    assert "valid integer" in retry_instructions[1]
    assert secret not in retry_instructions[1]
    assert secret not in validation_event.error
    assert secret not in manifest_text


def test_build_crew_defaults_to_three_analysts_without_masking():
    crew, task_map = build_crew(pdf_path="dummy.pdf")

    assert [task.name for task in crew.tasks] == [
        "case_bundle_generation",
        "analyst_alpha_analysis",
        "analyst_beta_analysis",
        "analyst_gamma_analysis",
    ]
    assert set(task_map) == {
        "case_bundle_raw",
        "case_bundle",
        "analyst_alpha",
        "analyst_beta",
        "analyst_gamma",
    }
    assert (
        "Never quote, restate, compare against, or discuss any author-reported"
        in crew.tasks[1].description
    )
    assert (
        "Return a complete SECTION A, SECTION B, and fenced SECTION C JSON."
        in crew.tasks[2].description
    )
    assert "fenced SECTION C JSON" in crew.tasks[2].expected_output


def test_build_crew_consumes_the_versioned_instruction_contract():
    crew, task_map = build_crew(pdf_path="dummy.pdf")
    task = task_map["analyst_alpha"]
    contract = build_analyst_instruction_contract(
        analyst_label="Analyst Alpha",
        prompt_text=load_rucam_prompt(),
        model_name=task.agent.llm.model,
        bundle_input_name="prepared_case_bundle_json",
    )

    assert task.agent.role == contract.agent_role
    assert task.agent.goal == contract.agent_goal
    assert task.agent.backstory == contract.agent_backstory
    assert task.description == contract.render_task_description(retry_instruction=None)
    assert task.expected_output == contract.task_expected_output


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
        assert all(
            getattr(task, "name", None) != "case_bundle_generation" for task in context
        )


def test_prepare_case_bundle_json_masks_deterministically(monkeypatch):
    class DummyBundle:
        def to_dict(self):
            return {
                "pdf_path": "example.pdf",
                "extraction_notes": [],
                "blocks": [
                    {
                        "element_type": "NarrativeText",
                        "page_number": 1,
                        "text": "RUCAM score 8",
                    }
                ],
                "normalized_text": "RUCAM score 8",
                "tables": [],
                "unknowns": [],
                "quality": {
                    "unstructured_total_score": 1,
                    "fallback_pages": [],
                    "fallback_total_score": 0,
                },
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
                "blocks": [
                    {
                        "element_type": "NarrativeText",
                        "page_number": 1,
                        "text": "RUCAM score 8",
                    }
                ],
                "normalized_text": "RUCAM score 8",
                "tables": [],
                "unknowns": [],
                "quality": {
                    "unstructured_total_score": 1,
                    "fallback_pages": [],
                    "fallback_total_score": 0,
                },
            }

    class DummyCrew:
        def __init__(self, label):
            self.label = label

        def kickoff(self, inputs):
            captured_inputs.append({"label": self.label, **inputs})
            return complete_report(narrative=self.label)

    class DummyTask:
        output = None

    monkeypatch.setattr(
        "dili_rucam_agents.crew.crew.build_case_bundle", lambda _: DummyBundle()
    )
    monkeypatch.setattr(
        "dili_rucam_agents.crew.crew.get_enabled_analyst_configs",
        lambda **kwargs: [
            {"key": "analyst_alpha", "label": "Analyst Alpha"},
            {"key": "analyst_beta", "label": "Analyst Beta"},
        ],
    )
    monkeypatch.setattr(
        "dili_rucam_agents.crew.crew._build_isolated_analyst_run",
        lambda *, config, **kwargs: (
            DummyCrew(config["key"].removeprefix("analyst_")),
            DummyTask(),
        ),
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
                "blocks": [
                    {
                        "element_type": "NarrativeText",
                        "page_number": 1,
                        "text": "RUCAM score 8",
                    }
                ],
                "normalized_text": "RUCAM score 8",
                "tables": [],
                "unknowns": [],
                "quality": {
                    "unstructured_total_score": 1,
                    "fallback_pages": [],
                    "fallback_total_score": 0,
                },
            }

    class DummyCrew:
        def __init__(self, label):
            self.label = label

        def kickoff(self, inputs):
            captured_inputs.append({"label": self.label, **inputs})
            return complete_report(narrative=self.label)

    class DummyTask:
        output = None

    monkeypatch.setattr(
        "dili_rucam_agents.crew.crew.build_case_bundle", lambda _: DummyBundle()
    )
    monkeypatch.setattr(
        "dili_rucam_agents.crew.crew.get_enabled_analyst_configs",
        lambda **kwargs: [{"key": "analyst_alpha", "label": "Analyst Alpha"}],
    )
    monkeypatch.setattr(
        "dili_rucam_agents.crew.crew._build_isolated_analyst_run",
        lambda *, config, **kwargs: (
            DummyCrew(config["key"].removeprefix("analyst_")),
            DummyTask(),
        ),
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
                "blocks": [
                    {
                        "element_type": "NarrativeText",
                        "page_number": 1,
                        "text": "RUCAM score 8",
                    }
                ],
                "normalized_text": "RUCAM score 8",
                "tables": [],
                "unknowns": [],
                "quality": {
                    "unstructured_total_score": 1,
                    "fallback_pages": [],
                    "fallback_total_score": 0,
                },
            }

    class DummyCrew:
        def __init__(self, label):
            self.label = label

        def kickoff(self, inputs):
            captured_inputs.append({"label": self.label, **inputs})
            return complete_report(narrative=self.label)

    class DummyTask:
        output = None

    monkeypatch.setattr(
        "dili_rucam_agents.crew.crew.build_case_bundle", lambda _: DummyBundle()
    )
    monkeypatch.setattr(
        "dili_rucam_agents.crew.crew.get_enabled_analyst_configs",
        lambda **kwargs: [{"key": "analyst_alpha", "label": "Analyst Alpha"}],
    )
    monkeypatch.setattr(
        "dili_rucam_agents.crew.crew._build_isolated_analyst_run",
        lambda *, config, **kwargs: (
            DummyCrew(config["key"].removeprefix("analyst_")),
            DummyTask(),
        ),
    )

    run_crew("dummy.pdf", enable_score_masking=False)

    assert len(captured_inputs) == 1
    assert "RUCAM score 8" in captured_inputs[0]["raw_case_bundle_json"]
    assert "masked_case_bundle_json" not in captured_inputs[0]
    assert "prepared_case_bundle_json" not in captured_inputs[0]


def test_run_crew_raises_when_analyst_returns_empty_output(monkeypatch):
    class DummyBundle:
        def to_dict(self):
            return {
                "pdf_path": "example.pdf",
                "extraction_notes": [],
                "blocks": [],
                "normalized_text": "ALT 650",
                "tables": [],
                "unknowns": [],
                "quality": {
                    "unstructured_total_score": 1,
                    "fallback_pages": [],
                    "fallback_total_score": 0,
                },
            }

    class DummyCrew:
        def kickoff(self, inputs):
            return "   "

    class DummyTask:
        output = None

    monkeypatch.setattr(
        "dili_rucam_agents.crew.crew.build_case_bundle", lambda _: DummyBundle()
    )
    monkeypatch.setattr(
        "dili_rucam_agents.crew.crew.get_enabled_analyst_configs",
        lambda **kwargs: [{"key": "analyst_zeta", "label": "Analyst Zeta"}],
    )
    monkeypatch.setattr(
        "dili_rucam_agents.crew.crew._build_isolated_analyst_run",
        lambda **kwargs: (DummyCrew(), DummyTask()),
    )

    with pytest.raises(AnalystExecutionError) as exc_info:
        run_crew("dummy.pdf", capture_reports=True, max_restarts=0)

    assert exc_info.value.analyst_key == "analyst_zeta"
    assert exc_info.value.failure_kind == "validation"


def test_run_crew_uses_kickoff_output_when_task_output_is_missing(monkeypatch):
    class DummyBundle:
        def to_dict(self):
            return {
                "pdf_path": "example.pdf",
                "extraction_notes": [],
                "blocks": [],
                "normalized_text": "ALT 650",
                "tables": [],
                "unknowns": [],
                "quality": {
                    "unstructured_total_score": 1,
                    "fallback_pages": [],
                    "fallback_total_score": 0,
                },
            }

    class DummyCrew:
        def kickoff(self, inputs):
            return complete_report(narrative="Report")

    class DummyTask:
        output = None

    monkeypatch.setattr(
        "dili_rucam_agents.crew.crew.build_case_bundle", lambda _: DummyBundle()
    )
    monkeypatch.setattr(
        "dili_rucam_agents.crew.crew.get_enabled_analyst_configs",
        lambda **kwargs: [{"key": "analyst_zeta", "label": "Analyst Zeta"}],
    )
    monkeypatch.setattr(
        "dili_rucam_agents.crew.crew._build_isolated_analyst_run",
        lambda **kwargs: (DummyCrew(), DummyTask()),
    )

    _, reports = run_crew("dummy.pdf", capture_reports=True)

    assert reports["analyst_zeta"] == complete_report(narrative="Report")


def test_run_crew_skips_existing_analyst_reports_on_retry(monkeypatch):
    captured_labels = []

    class DummyBundle:
        def to_dict(self):
            return {
                "pdf_path": "example.pdf",
                "extraction_notes": [],
                "blocks": [],
                "normalized_text": "ALT 650",
                "tables": [],
                "unknowns": [],
                "quality": {
                    "unstructured_total_score": 1,
                    "fallback_pages": [],
                    "fallback_total_score": 0,
                },
            }

    class DummyCrew:
        def __init__(self, label):
            self.label = label

        def kickoff(self, inputs):
            captured_labels.append(self.label)
            return complete_report(narrative=self.label)

    class DummyTask:
        output = None

    monkeypatch.setattr(
        "dili_rucam_agents.crew.crew.build_case_bundle", lambda _: DummyBundle()
    )
    monkeypatch.setattr(
        "dili_rucam_agents.crew.crew.get_enabled_analyst_configs",
        lambda **kwargs: [
            {"key": "analyst_alpha", "label": "Analyst Alpha"},
            {"key": "analyst_beta", "label": "Analyst Beta"},
            {"key": "analyst_gamma", "label": "Analyst Gamma"},
        ],
    )
    monkeypatch.setattr(
        "dili_rucam_agents.crew.crew._build_isolated_analyst_run",
        lambda *, config, **kwargs: (
            DummyCrew(config["key"].removeprefix("analyst_")),
            DummyTask(),
        ),
    )

    _, reports = run_crew(
        "dummy.pdf",
        capture_reports=True,
        completed_reports={
            "analyst_alpha": complete_report(narrative="existing alpha")
        },
    )

    assert captured_labels == ["beta", "gamma"]
    assert reports["analyst_alpha"] == complete_report(narrative="existing alpha")
    assert reports["analyst_beta"] == complete_report(narrative="beta")
    assert reports["analyst_gamma"] == complete_report(narrative="gamma")


def test_run_crew_leaves_analyst_reports_untouched_when_masking_enabled(monkeypatch):
    class DummyBundle:
        def to_dict(self):
            return {
                "pdf_path": "example.pdf",
                "extraction_notes": [],
                "blocks": [
                    {
                        "element_type": "NarrativeText",
                        "page_number": 1,
                        "text": "RUCAM score 8",
                    }
                ],
                "normalized_text": "RUCAM score 8",
                "tables": [],
                "unknowns": [],
                "quality": {
                    "unstructured_total_score": 1,
                    "fallback_pages": [],
                    "fallback_total_score": 0,
                },
            }

    analyst_report = complete_report(
        narrative=(
            "The authors of the case report calculated a score of 8. "
            "Applying the strict standardized RUCAM rules yields a score of 5."
        )
    )

    class DummyOutput:
        raw = analyst_report

    class DummyTask:
        output = DummyOutput()

    class DummyCrew:
        def kickoff(self, inputs):
            return analyst_report

    monkeypatch.setattr(
        "dili_rucam_agents.crew.crew.build_case_bundle", lambda _: DummyBundle()
    )
    monkeypatch.setattr(
        "dili_rucam_agents.crew.crew.get_enabled_analyst_configs",
        lambda **kwargs: [{"key": "analyst_alpha", "label": "Analyst Alpha"}],
    )
    monkeypatch.setattr(
        "dili_rucam_agents.crew.crew._build_isolated_analyst_run",
        lambda **kwargs: (DummyCrew(), DummyTask()),
    )
    monkeypatch.setattr(
        "dili_rucam_agents.crew.crew._run_ground_truth_score_finder",
        lambda raw_json: "ground truth report",
    )

    final_output, reports = run_crew(
        "dummy.pdf", enable_score_masking=True, capture_reports=True
    )

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
        ),
    }

    _persist_reports(reports, tmp_path)

    content = (tmp_path / "masked-case-bundle_report.md").read_text(encoding="utf-8")
    ground_truth_content = (tmp_path / "ground-truth-rucam-score_report.md").read_text(
        encoding="utf-8"
    )
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
            "When it is applied, a score from -10 to +14 is obtained and is used to classify causality into five categories.\\n"
            'The Roussel UCLAF method was applied for an acute hepatocellular problem, with a final score of 8.",'
            '"tables":[],"unknowns":[],"quality":{"unstructured_total_score":0,"fallback_pages":[],"fallback_total_score":0}}'
        ),
        masked_case_bundle_payload=(
            '{"pdf_path":"example.pdf","extraction_notes":[],"blocks":[],"normalized_text":"'
            "When it is applied, a score from -10 to +14 is obtained and is used to classify causality into five categories.\\n"
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
