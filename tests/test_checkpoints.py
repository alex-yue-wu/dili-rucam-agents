import json
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
from dili_rucam_agents.checkpoints import CHECKPOINT_SCHEMA_VERSION
from dili_rucam_agents.contracts import build_analyst_instruction_contract
from dili_rucam_agents.diagnostics import (
    SafeExecutionDiagnostic,
    SafeValidationDiagnostic,
    build_execution_diagnostic,
)
from dili_rucam_agents.providers import StructuredMode
from dili_rucam_agents.roster import AnalystConfig, Provider
from dili_rucam_agents.runner import RetryCounters
from dili_rucam_agents.validators.rucam_assessment import RucamAssessment
from dili_rucam_agents.validators.rucam_json import ITEM_LEGAL_SCORES


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


def assessment_json() -> str:
    flags = {
        name: "not_documented"
        for name in (
            "hepatitis_a",
            "hepatitis_b",
            "hepatitis_c",
            "biliary_obstruction",
            "alcoholism",
            "hypotension_or_ischaemia",
            "underlying_disease",
            "acute_viral_infection",
        )
    }
    payload = {
        "injury_pattern": "hepatocellular",
        "r_ratio": 12.5,
        "r_ratio_basis": {
            "alt": 500.0,
            "alp": 120.0,
            "alt_uln": 40.0,
            "alp_uln": 120.0,
        },
        "onset_day": 10,
        "onset_anchor_dose": "first dose",
        "pre_flight": {
            name: "x"
            for name in (
                "suspect_drug",
                "onset",
                "r_ratio_pattern",
                "course_basis",
                "g1_count",
                "item_6_source",
                "scores_in_legal_sets",
                "total_sum",
                "category_match",
                "section_b_matches_section_c",
                "notes_complete",
            )
        },
        "item_5_causes": {**flags, "g1": 0},
        **{
            item: {"score": 0, "data_status": "reported", "rationale": "because"}
            for item in ITEM_LEGAL_SCORES
        },
    }
    RucamAssessment.model_validate(payload)  # the fixture must itself be valid
    return json.dumps(payload)


COUNTERS = RetryCounters(
    validation_retries=2,
    transient_retries=3,
    truncation_escalations=5,
    mode_downgrades=7,
    final_mode=StructuredMode.json_object,
)


def identity(key: str = "analyst_alpha", fingerprint: str = "fingerprint-a"):
    return AnalystIdentity(
        key=key,
        report_filename=f"{key.replace('_', '-')}_report.md",
        assessment_filename=f"{key.replace('_', '-')}_assessment.json",
        fingerprint=fingerprint,
    )


def complete(store, ident=None, *, attempt=1, report=None, counters=COUNTERS):
    """Record a completed analyst with a valid report and a valid sidecar."""
    store.record_completed(
        ident or identity(),
        attempt=attempt,
        report_text=report if report is not None else complete_report(),
        assessment_json=assessment_json(),
        counters=counters,
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
        AnalystConfig(
            key="analyst_alpha",
            label="Analyst Alpha",
            provider=Provider.openai,
            model="model-a",
            max_output_tokens=12000,
        ),
        AnalystConfig(
            key="analyst_beta",
            label="Analyst Beta",
            provider=Provider.google,
            model="model-b",
            max_output_tokens=12000,
        ),
    ]
    monkeypatch.delenv("ANALYST_MODEL", raising=False)
    contracts = {
        config.key: build_analyst_instruction_contract(
            analyst_label=config.label,
            prompt_text="Production prompt",
            model_name=config.model,
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
    changed_alpha = contracts["analyst_alpha"].model_copy(
        update={"expected_output": "Changed static output contract."}
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

    first_instructions = contract.render(
        raw_case_bundle_json="PATIENT-CASE-CONTENT",
        retry_requirement=contract.render_retry("first sanitized diagnostic"),
    )
    second_instructions = contract.render(
        raw_case_bundle_json="PATIENT-CASE-CONTENT",
        retry_requirement=contract.render_retry("second sanitized diagnostic"),
    )

    assert first_instructions != second_instructions
    assert "first sanitized diagnostic" in first_instructions
    assert "PATIENT-CASE-CONTENT" in first_instructions
    # Neither the case content nor the retry diagnostic is in the contract, so
    # neither can move the fingerprint the resume check compares.
    assert contract.sha256 == fingerprint
    assert "{raw_case_bundle_json}" in contract.instruction_template
    assert "PATIENT-CASE-CONTENT" not in contract.instruction_template
    assert "first sanitized diagnostic" not in contract.retry_instruction_template


def test_static_retry_wrapper_is_part_of_instruction_fingerprint():
    contract = build_analyst_instruction_contract(
        analyst_label="Analyst Alpha",
        prompt_text="Production prompt",
        model_name="model-a",
        bundle_input_name="raw_case_bundle_json",
    )

    changed = contract.model_copy(
        update={
            "retry_instruction_template": (
                contract.retry_instruction_template
                + "\nReturn every required field before stopping."
            )
        }
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
    complete(store, identity(), attempt=1, report=report)
    assert store.load_compatible_reports([identity()], resume=True) == {
        "analyst_alpha": report
    }
    assert (
        store.load_compatible_reports([identity(fingerprint="changed")], resume=True)
        == {}
    )


def test_read_only_loading_rejects_schema_v2_without_mutation(tmp_path: Path):
    secret = "MODEL-CONTROLLED-V2-MANIFEST-SECRET"
    report = complete_report()
    (tmp_path / "analyst-alpha_report.md").write_text(report, encoding="utf-8")
    manifest_path = tmp_path / "analyst_checkpoints.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "pdf_filename": "case.pdf",
                "pdf_sha256": "pdf",
                "enabled_analysts": ["analyst_alpha"],
                "analysts": {
                    "analyst_alpha": {
                        "status": "completed",
                        "report_file": "analyst-alpha_report.md",
                        "fingerprint": "fingerprint-a",
                        "last_error": secret,
                        "attempt_history": [
                            {
                                "failure_kind": "validation",
                                "error": secret,
                            }
                        ],
                    }
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    manifest_before = manifest_path.read_bytes()
    store = AnalystCheckpointStore(
        tmp_path,
        pdf_filename="case.pdf",
        pdf_sha256="pdf",
        enabled_analysts=("analyst_alpha",),
    )

    reports = store.load_compatible_reports(
        [identity()],
        resume=True,
        adopt_legacy=False,
        allow_manifest_updates=False,
    )

    assert reports == {}
    assert not store.all_completed([identity()])
    assert manifest_path.read_bytes() == manifest_before


def test_store_rejects_invalid_report_even_when_manifest_says_completed(tmp_path: Path):
    store = AnalystCheckpointStore(
        tmp_path,
        pdf_filename="case.pdf",
        pdf_sha256="pdf",
        enabled_analysts=("analyst_alpha",),
    )
    complete(store, identity(), attempt=1, report=complete_report())
    (tmp_path / "analyst-alpha_report.md").write_text("incomplete", encoding="utf-8")
    assert store.load_compatible_reports([identity()], resume=True) == {}


def test_resume_false_bypasses_completed_checkpoints(tmp_path: Path):
    store = AnalystCheckpointStore(
        tmp_path,
        pdf_filename="case.pdf",
        pdf_sha256="pdf",
        enabled_analysts=("analyst_alpha",),
    )
    complete(store, identity(), attempt=1, report=complete_report())
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
    complete(store, identity(), attempt=1, report=complete_report())
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
    )

    complete(store, identity(), attempt=2, report=complete_report())

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
        execution_diagnostic=build_execution_diagnostic(original_error),
    )

    manifest_text = (tmp_path / "analyst_checkpoints.json").read_text()
    assert secret not in manifest_text
    assert "forged display string" not in manifest_text
    assert "type=ProviderRequestError" in manifest_text
    assert "status_code=503" in manifest_text
    assert "errno=54" in manifest_text


def test_checkpoint_store_rejects_forged_structured_diagnostic_before_any_write(
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
    secret = "sk-live-TOKEN-DO-NOT-STORE"
    forged = object.__new__(SafeExecutionDiagnostic)
    object.__setattr__(forged, "exception_type", f"ProviderError; token={secret}")
    object.__setattr__(forged, "status_code", 503)
    object.__setattr__(forged, "errno", None)

    invalid_diagnostics = (
        (forged, "exception_type"),
        ({"exception_type": f"ProviderError; token={secret}"}, "execution_diagnostic"),
    )
    for invalid_diagnostic, expected_error in invalid_diagnostics:
        with pytest.raises(ValueError, match=expected_error):
            store.record_failure(
                identity(),
                attempt=1,
                failure_kind="execution",
                error=f"forged display string with {secret}",
                execution_diagnostic=invalid_diagnostic,
            )

        assert manifest_path.read_bytes() == manifest_before
        assert not (tmp_path / "attempts").exists()


def test_checkpoint_store_revalidates_validation_diagnostic_before_any_write(
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
    secret = "MODEL-CONTROLLED-CHECKPOINT-SECRET"
    forged = object.__new__(SafeValidationDiagnostic)
    object.__setattr__(forged, "issues", (("total_score", secret),))

    with pytest.raises(ValueError):
        store.record_failure(
            identity(),
            attempt=1,
            failure_kind="validation",
            error=f"caller text {secret}",
            validation_diagnostic=forged,
        )

    assert manifest_path.read_bytes() == manifest_before
    assert not (tmp_path / "attempts").exists()

    store.record_failure(
        identity(),
        attempt=1,
        failure_kind="validation",
        error=f"caller text {secret}",
        validation_diagnostic=SafeValidationDiagnostic(
            (("total_score", "score_sum_mismatch"),)
        ),
    )

    manifest_text = manifest_path.read_text(encoding="utf-8")
    assert secret not in manifest_text
    assert "total_score does not match" in manifest_text
    # The store persists the diagnostic and nothing else; the rejected reply is
    # run_analysts' attempts/<key>.<n>.invalid.* artifact.
    assert not (tmp_path / "attempts").exists()


def test_checkpoint_store_never_executes_hostile_validation_issues_before_write(
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
    secret = "HOSTILE-CHECKPOINT-ISSUES-SECRET"

    class HostileIssues:
        def __init__(self):
            self.invoked = []

        def _raise(self, hook):
            self.invoked.append(hook)
            raise RuntimeError(f"{hook}: {secret}")

        def __iter__(self):
            return self._raise("__iter__")

        def __len__(self):
            return self._raise("__len__")

        def __repr__(self):
            return self._raise("__repr__")

    issues = HostileIssues()
    forged = object.__new__(SafeValidationDiagnostic)
    object.__setattr__(forged, "issues", issues)

    with pytest.raises(ValueError) as exc_info:
        store.record_failure(
            identity(),
            attempt=1,
            failure_kind="validation",
            error=f"caller text {secret}",
            validation_diagnostic=forged,
        )

    assert issues.invoked == []
    assert secret not in str(exc_info.value)
    assert manifest_path.read_bytes() == manifest_before
    assert not (tmp_path / "attempts").exists()


def test_checkpoint_store_rejects_non_finite_report_before_any_mutation(
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
    non_finite_report = complete_report().replace("6.4", "Infinity", 1)

    with pytest.raises(ValueError):
        complete(store, identity(), attempt=1, report=non_finite_report)

    assert manifest_path.read_bytes() == manifest_before
    assert not (tmp_path / identity().report_filename).exists()
    assert not (tmp_path / identity().assessment_filename).exists()


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
    complete(alpha_store, identity(), attempt=1, report=complete_report())

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
    complete(expanded_store, alpha, attempt=1, report=complete_report())
    complete(expanded_store, delta, attempt=1, report=complete_report())

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
    complete(expanded_store, alpha, attempt=1, report=complete_report())
    complete(expanded_store, delta, attempt=1, report=complete_report())
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
    (tmp_path / "analyst-alpha_assessment.json").write_text(
        assessment_json(), encoding="utf-8"
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
            pdf_filename="case.pdf",
            masking_enabled=False,
            strict_scoring=False,
        ),
    )
    assert reports == {"analyst_alpha": complete_report()}
    assert (tmp_path / "analyst_checkpoints.json").exists()


def test_store_does_not_adopt_tolerant_unfenced_historical_report(tmp_path: Path):
    unfenced_report = (
        complete_report().replace("```json\n", "").replace("\n```\n", "\n")
    )
    (tmp_path / "analyst-alpha_report.md").write_text(unfenced_report, encoding="utf-8")
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

    assert reports == {}
    assert not (tmp_path / "analyst_checkpoints.json").exists()


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


def test_store_does_not_adopt_a_legacy_report_that_has_no_sidecar(tmp_path: Path):
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
        legacy_context=LegacyRunContext("case.pdf", False, False),
    )
    assert reports == {}
    assert not (tmp_path / "analyst_checkpoints.json").exists()


def make_store(tmp_path: Path, *analysts: str) -> AnalystCheckpointStore:
    return AnalystCheckpointStore(
        tmp_path,
        pdf_filename="case.pdf",
        pdf_sha256="pdf",
        enabled_analysts=analysts or ("analyst_alpha",),
    )


def test_schema_version_is_four():
    assert CHECKPOINT_SCHEMA_VERSION == 4
    assert make_store(Path("."))._new_manifest()["schema_version"] == 4


def test_a_manifest_at_version_three_is_discarded(tmp_path: Path):
    store = make_store(tmp_path)
    complete(store)
    manifest_path = tmp_path / "analyst_checkpoints.json"
    manifest = json.loads(manifest_path.read_text())
    assert manifest["schema_version"] == 4
    # A v3 manifest that otherwise matches in every field, artifacts included.
    manifest["schema_version"] = 3
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    before = manifest_path.read_bytes()

    reader = make_store(tmp_path)
    assert reader.load_compatible_reports(
        [identity()], resume=True, adopt_legacy=False, allow_manifest_updates=False
    ) == {}
    assert not reader.all_completed([identity()])
    # Discarded, not migrated: nothing rewrote it.
    assert manifest_path.read_bytes() == before


def test_completed_entry_records_the_four_counters(tmp_path: Path):
    store = make_store(tmp_path)
    complete(store, attempt=3)

    entry = json.loads((tmp_path / "analyst_checkpoints.json").read_text())[
        "analysts"
    ]["analyst_alpha"]
    assert entry["validation_retries"] == 2
    assert entry["transient_retries"] == 3
    assert entry["truncation_escalations"] == 5
    assert entry["mode_downgrades"] == 7
    assert entry["final_mode"] == "json_object"
    assert entry["assessment_file"] == "analyst-alpha_assessment.json"


def test_counters_accumulate_across_invocations(tmp_path: Path):
    """A resumed invocation must not erase what the failed one spent.

    The failed invocation spent its whole validation budget; the resume
    succeeds with none. Last-write-wins would report the 0 and lose the 2, in
    the direction that passes an acceptance bar, so the four counts add up.
    """
    store = make_store(tmp_path)
    spent = RetryCounters(
        validation_retries=2,
        transient_retries=1,
        truncation_escalations=1,
        mode_downgrades=3,
        final_mode=StructuredMode.text,
    )
    store.record_running(identity(), attempt=1)
    store.record_failure(
        identity(),
        attempt=3,
        failure_kind="validation",
        error="ignored",
        counters=spent,
    )

    # Second invocation: succeeds first time, so it spends nothing.
    clean = RetryCounters(0, 0, 0, 0, StructuredMode.json_schema)
    store.record_running(identity(), attempt=1)
    complete(store, attempt=1, counters=clean)

    entry = json.loads((tmp_path / "analyst_checkpoints.json").read_text())[
        "analysts"
    ]["analyst_alpha"]
    assert entry["status"] == "completed"
    assert entry["validation_retries"] == 2
    assert entry["transient_retries"] == 1
    assert entry["truncation_escalations"] == 1
    assert entry["mode_downgrades"] == 3
    # final_mode is a state, not a count: the last invocation's mode stands.
    assert entry["final_mode"] == "json_schema"
    # And the breakdown says which invocation spent them.
    (failed,) = entry["attempt_history"]
    assert failed["validation_retries"] == 2
    assert failed["mode_downgrades"] == 3
    assert failed["final_mode"] == "text"


def test_a_history_entry_invents_no_counter_block(tmp_path: Path):
    """An intermediate rejected attempt carries no counters; none are made up."""
    store = make_store(tmp_path)
    store.record_running(identity(), attempt=1)
    store.record_failure(
        identity(), attempt=1, failure_kind="validation", error="ignored"
    )

    entry = json.loads((tmp_path / "analyst_checkpoints.json").read_text())[
        "analysts"
    ]["analyst_alpha"]
    (only,) = entry["attempt_history"]
    for key in ("validation_retries", "transient_retries", "mode_downgrades"):
        assert key not in only
        assert key not in entry


def test_a_corrupt_counter_total_reads_as_zero(tmp_path: Path):
    """A hand-edited manifest must not make the next write raise."""
    store = make_store(tmp_path)
    complete(store)
    manifest_path = tmp_path / "analyst_checkpoints.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["analysts"]["analyst_alpha"]["validation_retries"] = "two"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    complete(store)
    entry = json.loads(manifest_path.read_text())["analysts"]["analyst_alpha"]
    assert entry["validation_retries"] == 2


def test_failure_entry_records_the_counters_and_the_new_kinds(tmp_path: Path):
    store = make_store(tmp_path)
    store.record_running(identity(), attempt=1)
    store.record_failure(
        identity(),
        attempt=1,
        failure_kind="truncation",
        error="ignored",
        counters=COUNTERS,
    )
    entry = json.loads((tmp_path / "analyst_checkpoints.json").read_text())[
        "analysts"
    ]["analyst_alpha"]
    assert entry["failure_kind"] == "truncation"
    assert "cut off" in entry["last_error"]
    assert (entry["validation_retries"], entry["mode_downgrades"]) == (2, 7)
    assert entry["final_mode"] == "json_object"

    store.record_failure(
        identity(),
        attempt=1,
        failure_kind="terminal",
        error="ignored",
        execution_diagnostic=SafeExecutionDiagnostic("AuthenticationError", 401),
    )
    entry = json.loads((tmp_path / "analyst_checkpoints.json").read_text())[
        "analysts"
    ]["analyst_alpha"]
    assert entry["failure_kind"] == "terminal"
    assert entry["last_error"] == (
        "Execution error [type=AuthenticationError; status_code=401]"
    )
    with pytest.raises(ValueError, match="cannot carry"):
        store.record_failure(
            identity(),
            attempt=1,
            failure_kind="terminal",
            error="x",
            validation_diagnostic=SafeValidationDiagnostic((("report", "invalid_report"),)),
        )


def test_completed_requires_both_the_report_and_the_sidecar(tmp_path: Path):
    store = make_store(tmp_path)
    complete(store)
    sidecar = tmp_path / "analyst-alpha_assessment.json"
    assert store.all_completed([identity()])

    sidecar.unlink()

    # The report is still on disk and still valid; the analyst must be re-scored.
    assert (tmp_path / "analyst-alpha_report.md").exists()
    assert not store.all_completed([identity()])
    assert store.load_compatible_reports([identity()], resume=True) == {}

    sidecar.write_text("{not json", encoding="utf-8")
    assert store.load_compatible_reports([identity()], resume=True) == {}

    # An object that parses but is not a valid assessment does not count either.
    sidecar.write_text(json.dumps({"injury_pattern": "hepatocellular"}), encoding="utf-8")
    assert store.load_compatible_reports([identity()], resume=True) == {}

    sidecar.write_text(assessment_json(), encoding="utf-8")
    assert store.all_completed([identity()])


def test_a_missing_report_alone_also_forces_a_rescore(tmp_path: Path):
    store = make_store(tmp_path)
    complete(store)
    (tmp_path / "analyst-alpha_report.md").unlink()
    assert not store.all_completed([identity()])


def test_record_completed_rejects_an_invalid_assessment_before_any_write(
    tmp_path: Path,
):
    store = make_store(tmp_path)
    store.record_running(identity(), attempt=1)
    manifest_path = tmp_path / "analyst_checkpoints.json"
    before = manifest_path.read_bytes()
    secret = "MODEL-CONTROLLED-SIDECAR-SECRET"

    for bad in (json.dumps({"injury_pattern": secret}), "{", "[]"):
        with pytest.raises(ValueError) as info:
            store.record_completed(
                identity(),
                attempt=1,
                report_text=complete_report(),
                assessment_json=bad,
                counters=COUNTERS,
            )
        assert secret not in str(info.value)
    with pytest.raises(TypeError):
        store.record_completed(
            identity(),
            attempt=1,
            report_text=complete_report(),
            assessment_json={"not": "text"},
            counters=COUNTERS,
        )

    assert manifest_path.read_bytes() == before
    assert not (tmp_path / identity().report_filename).exists()
    assert not (tmp_path / identity().assessment_filename).exists()


def test_the_sidecar_holds_the_assessment_verbatim(tmp_path: Path):
    store = make_store(tmp_path)
    complete(store)
    assert (tmp_path / "analyst-alpha_assessment.json").read_text() == assessment_json()


def test_removing_an_analyst_leaves_the_others_checkpoints_valid(tmp_path: Path):
    alpha, delta = identity(), identity("analyst_delta", "fingerprint-d")
    full = make_store(tmp_path, "analyst_alpha", "analyst_delta")
    complete(full, alpha)
    complete(full, delta)
    assert full.all_completed([alpha, delta])

    reduced = make_store(tmp_path, "analyst_alpha")
    assert reduced.load_compatible_reports([alpha], resume=True) == {
        "analyst_alpha": complete_report()
    }
    assert reduced.all_completed([alpha])
    manifest = json.loads((tmp_path / "analyst_checkpoints.json").read_text())
    assert manifest["enabled_analysts"] == ["analyst_alpha"]
    assert manifest["analysts"]["analyst_alpha"]["status"] == "completed"


def test_fingerprint_ignores_the_roster():
    def configs(*keys: str) -> list[AnalystConfig]:
        return [
            AnalystConfig(
                key=key,
                label=key,
                provider=Provider.openai,
                model=f"model-{key}",
                max_output_tokens=12000,
            )
            for key in keys
        ]

    def identities(*keys: str):
        return build_analyst_identities(
            configs=configs(*keys),
            report_filename_map={k: f"{k.replace('_', '-')}_report.md" for k in keys},
            pdf_sha256="pdf-a",
            analyst_instruction_sha256={k: f"instr-{k}" for k in keys},
            enable_score_masking=False,
            strict_scoring=False,
        )

    both = identities("analyst_alpha", "analyst_beta")
    alone = identities("analyst_alpha")
    reordered = identities("analyst_beta", "analyst_alpha")
    assert alone["analyst_alpha"] == both["analyst_alpha"] == reordered["analyst_alpha"]
    assert both["analyst_alpha"].fingerprint != both["analyst_beta"].fingerprint


def test_identity_reads_model_and_budget_off_the_config():
    def fingerprint(model: str, budget: int) -> str:
        config = AnalystConfig(
            key="analyst_alpha",
            label="Alpha",
            provider=Provider.openai,
            model=model,
            max_output_tokens=budget,
        )
        return build_analyst_identities(
            configs=[config],
            report_filename_map={"analyst_alpha": "analyst-alpha_report.md"},
            pdf_sha256="p",
            analyst_instruction_sha256={"analyst_alpha": "i"},
            enable_score_masking=False,
            strict_scoring=False,
        )["analyst_alpha"].fingerprint

    base = fingerprint("m", 1000)
    assert fingerprint("other", 1000) != base
    assert fingerprint("m", 2000) != base
    assert fingerprint("m", 1000) == base
