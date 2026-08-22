import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from dili_rucam_agents.checkpoints import (
    AnalystCheckpointStore,
    AnalystIdentity,
    LegacyRunContext,
    atomic_write_text,
    build_analyst_fingerprint,
)


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
        ("prompt_sha256", "prompt-b"),
        ("enable_score_masking", True),
        ("strict_scoring", True),
        ("model", "model-b"),
        ("max_output_tokens", 16000),
    ],
)
def test_fingerprint_changes_with_each_compatibility_input(field, replacement):
    base = dict(
        pdf_sha256="pdf-a",
        prompt_sha256="prompt-a",
        enable_score_masking=False,
        strict_scoring=False,
        model="model-a",
        max_output_tokens=12000,
    )
    first = build_analyst_fingerprint(**base)
    assert build_analyst_fingerprint(**{**base, field: replacement}) != first


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
    assert store.load_compatible_reports(
        [identity(fingerprint="changed")], resume=True
    ) == {}


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
