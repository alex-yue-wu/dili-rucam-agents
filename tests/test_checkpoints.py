import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

import dili_rucam_agents.diagnostics as diagnostics_module
from dili_rucam_agents.checkpoints import (
    AnalystCheckpointStore,
    AnalystIdentity,
    LegacyRunContext,
    atomic_write_text,
    build_analyst_fingerprint,
    build_analyst_identities,
)
from dili_rucam_agents.crew.tasks import build_analyst_instruction_contract


def complete_report() -> str:
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
        "total_score": 6,
        "category": "Probable",
    }
    return (
        "## SECTION A\n\nClinical summary\n\n"
        "## SECTION B\n\n| Item | Score |\n| --- | --- |\n| Total | 6 |\n\n"
        f"## SECTION C\n\n```json\n{json.dumps(payload)}\n```\n"
    )


def identity(key: str = "analyst_alpha", fingerprint: str = "fingerprint-a"):
    return AnalystIdentity(
        key=key,
        report_filename=f"{key.replace('_', '-')}_report.md",
        fingerprint=fingerprint,
    )


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("pdf_sha256", "pdf-b"),
        ("analyst_instruction_sha256", "instructions-b"),
        ("enable_score_masking", True),
        ("strict_scoring", True),
        ("model", "model-b"),
        ("max_output_tokens", 16000),
    ],
)
def test_fingerprint_changes_with_each_compatibility_input(field, replacement):
    base = dict(
        pdf_sha256="pdf-a",
        analyst_instruction_sha256="instructions-a",
        enable_score_masking=False,
        strict_scoring=False,
        model="model-a",
        max_output_tokens=12000,
    )
    first = build_analyst_fingerprint(**base)
    assert build_analyst_fingerprint(**{**base, field: replacement}) != first


def test_static_instruction_change_invalidates_only_affected_analyst(monkeypatch):
    configs = [
        {
            "key": "analyst_alpha",
            "label": "Analyst Alpha",
            "model_env": "TEST_ALPHA_MODEL",
            "max_tokens_env": "TEST_ALPHA_MAX_TOKENS",
            "fallback_envs": (),
            "default_model": "model-a",
        },
        {
            "key": "analyst_beta",
            "label": "Analyst Beta",
            "model_env": "TEST_BETA_MODEL",
            "max_tokens_env": "TEST_BETA_MAX_TOKENS",
            "fallback_envs": (),
            "default_model": "model-b",
        },
    ]
    monkeypatch.delenv("ANALYST_MODEL", raising=False)
    contracts = {
        config["key"]: build_analyst_instruction_contract(
            analyst_label=config["label"],
            prompt_text="Production prompt",
            model_name=config["default_model"],
            bundle_input_name="raw_case_bundle_json",
        )
        for config in configs
    }
    report_filename_map = {
        "analyst_alpha": "analyst-alpha_report.md",
        "analyst_beta": "analyst-beta_report.md",
    }

    original = build_analyst_identities(
        configs=configs,
        report_filename_map=report_filename_map,
        pdf_sha256="pdf-a",
        analyst_instruction_sha256={
            key: contract.sha256 for key, contract in contracts.items()
        },
        enable_score_masking=False,
        strict_scoring=False,
    )
    changed_alpha = replace(
        contracts["analyst_alpha"],
        task_expected_output="Changed static output contract.",
    )
    changed = build_analyst_identities(
        configs=configs,
        report_filename_map=report_filename_map,
        pdf_sha256="pdf-a",
        analyst_instruction_sha256={
            "analyst_alpha": changed_alpha.sha256,
            "analyst_beta": contracts["analyst_beta"].sha256,
        },
        enable_score_masking=False,
        strict_scoring=False,
    )

    assert changed["analyst_alpha"].fingerprint != original["analyst_alpha"].fingerprint
    assert changed["analyst_beta"].fingerprint == original["analyst_beta"].fingerprint


def test_instruction_fingerprint_excludes_case_content_and_retry_diagnostics():
    contract = build_analyst_instruction_contract(
        analyst_label="Analyst Alpha",
        prompt_text="Production prompt",
        model_name="model-a",
        bundle_input_name="raw_case_bundle_json",
    )
    fingerprint = contract.sha256

    first_description = contract.render_task_description(
        retry_instruction="first sanitized diagnostic"
    )
    second_description = contract.render_task_description(
        retry_instruction="second sanitized diagnostic"
    )

    assert first_description != second_description
    assert contract.sha256 == fingerprint
    assert "{raw_case_bundle_json}" in first_description
    assert "PATIENT-CASE-CONTENT" not in first_description


def test_static_retry_wrapper_is_part_of_instruction_fingerprint():
    contract = build_analyst_instruction_contract(
        analyst_label="Analyst Alpha",
        prompt_text="Production prompt",
        model_name="model-a",
        bundle_input_name="raw_case_bundle_json",
    )

    changed = replace(
        contract,
        retry_instruction_template=(
            contract.retry_instruction_template
            + "\nReturn every required field before stopping."
        ),
    )

    assert changed.sha256 != contract.sha256


def test_store_reuses_only_matching_completed_valid_report(tmp_path: Path):
    store = AnalystCheckpointStore(
        tmp_path,
        pdf_filename="case.pdf",
        pdf_sha256="pdf",
        enabled_analysts=("analyst_alpha",),
    )
    report = complete_report()
    store.record_completed(identity(), attempt=1, report_text=report)
    assert store.load_compatible_reports([identity()], resume=True) == {
        "analyst_alpha": report
    }
    assert (
        store.load_compatible_reports([identity(fingerprint="changed")], resume=True)
        == {}
    )


def test_store_rejects_invalid_report_even_when_manifest_says_completed(tmp_path: Path):
    store = AnalystCheckpointStore(
        tmp_path,
        pdf_filename="case.pdf",
        pdf_sha256="pdf",
        enabled_analysts=("analyst_alpha",),
    )
    store.record_completed(identity(), attempt=1, report_text=complete_report())
    (tmp_path / "analyst-alpha_report.md").write_text("incomplete", encoding="utf-8")
    assert store.load_compatible_reports([identity()], resume=True) == {}


def test_resume_false_bypasses_completed_checkpoints(tmp_path: Path):
    store = AnalystCheckpointStore(
        tmp_path,
        pdf_filename="case.pdf",
        pdf_sha256="pdf",
        enabled_analysts=("analyst_alpha",),
    )
    store.record_completed(identity(), attempt=1, report_text=complete_report())
    assert store.load_compatible_reports([identity()], resume=False) == {}


def test_running_or_corrupt_checkpoint_is_not_reused(tmp_path: Path):
    store = AnalystCheckpointStore(
        tmp_path,
        pdf_filename="case.pdf",
        pdf_sha256="pdf",
        enabled_analysts=("analyst_alpha",),
    )
    store.record_running(identity(), attempt=1)
    assert store.load_compatible_reports([identity()], resume=True) == {}
    (tmp_path / "analyst_checkpoints.json").write_text("{", encoding="utf-8")
    assert store.load_compatible_reports([identity()], resume=True) == {}


def test_adding_delta_preserves_alpha_checkpoint(tmp_path: Path):
    store = AnalystCheckpointStore(
        tmp_path,
        pdf_filename="case.pdf",
        pdf_sha256="pdf",
        enabled_analysts=("analyst_alpha", "analyst_delta"),
    )
    store.record_completed(identity(), attempt=1, report_text=complete_report())
    reports = store.load_compatible_reports(
        [identity(), identity("analyst_delta", "fingerprint-d")], resume=True
    )
    assert set(reports) == {"analyst_alpha"}


def test_total_attempts_accumulate_across_invocations(tmp_path: Path):
    store = AnalystCheckpointStore(
        tmp_path,
        pdf_filename="case.pdf",
        pdf_sha256="pdf",
        enabled_analysts=("analyst_alpha",),
    )
    store.record_running(identity(), attempt=1)
    store.record_failure(
        identity(), attempt=1, failure_kind="validation", error="missing SECTION C"
    )
    store.record_running(identity(), attempt=1)
    manifest = json.loads((tmp_path / "analyst_checkpoints.json").read_text())
    entry = manifest["analysts"]["analyst_alpha"]
    assert entry["total_attempts"] == 2
    assert entry["attempts_in_last_invocation"] == 1


def test_completion_clears_current_failure_fields_but_retains_history(tmp_path: Path):
    store = AnalystCheckpointStore(
        tmp_path,
        pdf_filename="case.pdf",
        pdf_sha256="pdf",
        enabled_analysts=("analyst_alpha",),
    )
    store.record_running(identity(), attempt=1)
    store.record_failure(
        identity(),
        attempt=1,
        failure_kind="validation",
        error="Missing SECTION C",
        report_text="invalid report",
    )

    store.record_completed(identity(), attempt=2, report_text=complete_report())

    manifest = json.loads((tmp_path / "analyst_checkpoints.json").read_text())
    entry = manifest["analysts"]["analyst_alpha"]
    assert entry["status"] == "completed"
    assert "failure_kind" not in entry
    assert "failed_at" not in entry
    assert entry["last_error"] is None
    assert len(entry["attempt_history"]) == 1
    assert entry["attempt_history"][0]["failure_kind"] == "validation"
    assert entry["attempt_history"][0]["failed_at"]


def test_checkpoint_store_canonicalizes_forged_execution_diagnostics(tmp_path: Path):
    store = AnalystCheckpointStore(
        tmp_path,
        pdf_filename="case.pdf",
        pdf_sha256="pdf",
        enabled_analysts=("analyst_alpha",),
    )
    secret = "sk-live-TOKEN-DO-NOT-STORE"

    class SecretObject:
        def __str__(self):
            return f"provider object containing {secret}"

    formatted_type = type(diagnostics_module.format_execution_error(Exception()))
    forged_marker = formatted_type(
        f"Execution error [type=ForgedError; token={secret}]"
    )
    for attempt, untrusted_error in enumerate((forged_marker, SecretObject()), start=1):
        store.record_running(identity(), attempt=attempt)
        store.record_failure(
            identity(),
            attempt=attempt,
            failure_kind="execution",
            error=untrusted_error,
        )

    manifest_text = (tmp_path / "analyst_checkpoints.json").read_text()
    assert secret not in manifest_text
    assert "ForgedError" not in manifest_text
    assert "provider object" not in manifest_text
    assert manifest_text.count("Execution error [type=Exception]") == 3


def test_checkpoint_store_derives_execution_diagnostic_from_original_exception(
    tmp_path: Path,
):
    store = AnalystCheckpointStore(
        tmp_path,
        pdf_filename="case.pdf",
        pdf_sha256="pdf",
        enabled_analysts=("analyst_alpha",),
    )
    secret = "sk-live-TOKEN-DO-NOT-STORE"

    class ProviderRequestError(RuntimeError):
        status_code = 503
        errno = 54

    original_error = ProviderRequestError(f"provider response with {secret}")
    store.record_running(identity(), attempt=1)
    store.record_failure(
        identity(),
        attempt=1,
        failure_kind="execution",
        error=f"forged display string with {secret}",
        execution_exception=original_error,
    )

    manifest_text = (tmp_path / "analyst_checkpoints.json").read_text()
    assert secret not in manifest_text
    assert "forged display string" not in manifest_text
    assert "type=ProviderRequestError" in manifest_text
    assert "status_code=503" in manifest_text
    assert "errno=54" in manifest_text


def test_checkpoint_store_rejects_invalid_failure_kind_before_any_write(
    tmp_path: Path,
):
    store = AnalystCheckpointStore(
        tmp_path,
        pdf_filename="case.pdf",
        pdf_sha256="pdf",
        enabled_analysts=("analyst_alpha",),
    )
    store.record_running(identity(), attempt=1)
    manifest_path = tmp_path / "analyst_checkpoints.json"
    manifest_before = manifest_path.read_bytes()

    with pytest.raises(ValueError, match="failure_kind"):
        store.record_failure(
            identity(),
            attempt=1,
            failure_kind="execuiton",
            error="unsafe",
            report_text="must not be written",
        )

    assert manifest_path.read_bytes() == manifest_before
    assert not (tmp_path / "attempts").exists()


def test_existing_manifest_write_refreshes_enabled_analysts(tmp_path: Path):
    alpha_store = AnalystCheckpointStore(
        tmp_path,
        pdf_filename="case.pdf",
        pdf_sha256="pdf",
        enabled_analysts=("analyst_alpha",),
    )
    alpha_store.record_completed(identity(), attempt=1, report_text=complete_report())

    expanded_store = AnalystCheckpointStore(
        tmp_path,
        pdf_filename="case.pdf",
        pdf_sha256="pdf",
        enabled_analysts=("analyst_alpha", "analyst_delta"),
    )
    expanded_store.record_running(identity("analyst_delta", "fingerprint-d"), attempt=1)

    manifest = json.loads((tmp_path / "analyst_checkpoints.json").read_text())
    assert manifest["enabled_analysts"] == ["analyst_alpha", "analyst_delta"]


def test_reusing_contracted_topology_refreshes_manifest_metadata(tmp_path: Path):
    expanded_store = AnalystCheckpointStore(
        tmp_path,
        pdf_filename="case.pdf",
        pdf_sha256="pdf",
        enabled_analysts=("analyst_alpha", "analyst_delta"),
    )
    alpha = identity()
    delta = identity("analyst_delta", "fingerprint-d")
    expanded_store.record_completed(alpha, attempt=1, report_text=complete_report())
    expanded_store.record_completed(delta, attempt=1, report_text=complete_report())

    contracted_store = AnalystCheckpointStore(
        tmp_path,
        pdf_filename="case.pdf",
        pdf_sha256="pdf",
        enabled_analysts=("analyst_alpha",),
    )
    reports = contracted_store.load_compatible_reports([alpha], resume=True)

    assert reports == {"analyst_alpha": complete_report()}
    manifest = json.loads((tmp_path / "analyst_checkpoints.json").read_text())
    assert manifest["enabled_analysts"] == ["analyst_alpha"]


def test_contracted_topology_read_only_loading_never_writes_manifest(
    tmp_path: Path, monkeypatch
):
    expanded_store = AnalystCheckpointStore(
        tmp_path,
        pdf_filename="case.pdf",
        pdf_sha256="pdf",
        enabled_analysts=("analyst_alpha", "analyst_delta"),
    )
    alpha = identity()
    delta = identity("analyst_delta", "fingerprint-d")
    expanded_store.record_completed(alpha, attempt=1, report_text=complete_report())
    expanded_store.record_completed(delta, attempt=1, report_text=complete_report())
    manifest_path = tmp_path / "analyst_checkpoints.json"
    manifest_before = manifest_path.read_bytes()

    read_only_store = AnalystCheckpointStore(
        tmp_path,
        pdf_filename="case.pdf",
        pdf_sha256="pdf",
        enabled_analysts=("analyst_alpha",),
    )
    writer = Mock(side_effect=AssertionError("read-only completion path wrote"))
    monkeypatch.setattr(read_only_store, "_write_manifest", writer)

    reports = read_only_store.load_compatible_reports(
        [alpha], resume=True, adopt_legacy=False
    )

    assert reports == {"analyst_alpha": complete_report()}
    assert read_only_store.all_completed([alpha])
    writer.assert_not_called()
    assert manifest_path.read_bytes() == manifest_before


def test_store_adopts_valid_legacy_report(tmp_path: Path):
    report_path = tmp_path / "analyst-alpha_report.md"
    report_path.write_text(complete_report(), encoding="utf-8")
    store = AnalystCheckpointStore(
        tmp_path,
        pdf_filename="case.pdf",
        pdf_sha256="pdf",
        enabled_analysts=("analyst_alpha",),
    )
    reports = store.load_compatible_reports(
        [identity()],
        resume=True,
        legacy_context=LegacyRunContext(
            pdf_filename="case.pdf",
            masking_enabled=False,
            strict_scoring=False,
        ),
    )
    assert reports == {"analyst_alpha": complete_report()}
    assert (tmp_path / "analyst_checkpoints.json").exists()


def test_store_rejects_legacy_context_for_a_different_pdf(tmp_path: Path):
    (tmp_path / "analyst-alpha_report.md").write_text(
        complete_report(), encoding="utf-8"
    )
    store = AnalystCheckpointStore(
        tmp_path,
        pdf_filename="case.pdf",
        pdf_sha256="pdf",
        enabled_analysts=("analyst_alpha",),
    )

    reports = store.load_compatible_reports(
        [identity()],
        resume=True,
        legacy_context=LegacyRunContext(
            pdf_filename="different-case.pdf",
            masking_enabled=False,
            strict_scoring=False,
        ),
    )

    assert reports == {}
    assert not (tmp_path / "analyst_checkpoints.json").exists()


def test_atomic_write_does_not_replace_destination_when_replace_fails(
    tmp_path, monkeypatch
):
    destination = tmp_path / "report.md"
    destination.write_text("old", encoding="utf-8")
    monkeypatch.setattr(
        "dili_rucam_agents.checkpoints.os.replace", Mock(side_effect=OSError("stop"))
    )
    with pytest.raises(OSError, match="stop"):
        atomic_write_text(destination, "new")
    assert destination.read_text(encoding="utf-8") == "old"
