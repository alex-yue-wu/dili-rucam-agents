from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from dili_rucam_agents.diagnostics import (
    SafeExecutionDiagnostic,
    SafeValidationDiagnostic,
    render_execution_diagnostic,
    render_validation_diagnostic,
    validate_failure_kind,
    validate_validation_diagnostic,
)
from dili_rucam_agents.roster import AnalystConfig
from dili_rucam_agents.runner import RetryCounters
from dili_rucam_agents.validators.analyst_report import validate_analyst_report
from dili_rucam_agents.validators.rucam_assessment import RucamAssessment


# 4: each analyst writes a validated-assessment sidecar beside its report, and
# the manifest records the runner's four retry counters. A version-3 manifest
# is discarded, not migrated.
CHECKPOINT_SCHEMA_VERSION = 4
CHECKPOINT_FILENAME = "analyst_checkpoints.json"


@dataclass(frozen=True)
class AnalystIdentity:
    key: str
    report_filename: str
    assessment_filename: str
    fingerprint: str


@dataclass(frozen=True)
class LegacyRunContext:
    pdf_filename: str
    masking_enabled: bool
    strict_scoring: bool


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def build_analyst_fingerprint(
    *,
    pdf_sha256: str,
    analyst_instruction_sha256: str,
    enable_score_masking: bool,
    strict_scoring: bool,
    model: str,
    max_output_tokens: int | None,
    hepatotoxicity_evidence_sha256: str | None = None,
) -> str:
    payload = {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "pdf_sha256": pdf_sha256,
        "analyst_instruction_sha256": analyst_instruction_sha256,
        "enable_score_masking": enable_score_masking,
        "strict_scoring": strict_scoring,
        "model": model,
        "max_output_tokens": max_output_tokens,
        # Item 6 evidence is resolved into the bundle, so a different cascade
        # configuration means a different analyst input.
        "hepatotoxicity_evidence_sha256": hepatotoxicity_evidence_sha256,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def assessment_filename_for(key: str) -> str:
    """``analyst_alpha`` -> ``analyst-alpha_assessment.json``, beside the report."""
    return f"{key.replace('_', '-')}_assessment.json"


def build_analyst_identities(
    *,
    configs: list[AnalystConfig],
    report_filename_map: Mapping[str, str],
    pdf_sha256: str,
    analyst_instruction_sha256: Mapping[str, str],
    enable_score_masking: bool,
    strict_scoring: bool,
    hepatotoxicity_evidence_sha256: str | None = None,
) -> dict[str, AnalystIdentity]:
    identities: dict[str, AnalystIdentity] = {}
    for config in configs:
        key = config.key
        identities[key] = AnalystIdentity(
            key=key,
            report_filename=report_filename_map[key],
            assessment_filename=assessment_filename_for(key),
            fingerprint=build_analyst_fingerprint(
                pdf_sha256=pdf_sha256,
                analyst_instruction_sha256=analyst_instruction_sha256[key],
                enable_score_masking=enable_score_masking,
                strict_scoring=strict_scoring,
                model=config.model,
                max_output_tokens=config.max_output_tokens,
                hepatotoxicity_evidence_sha256=hepatotoxicity_evidence_sha256,
            ),
        )
    return identities


class AnalystCheckpointStore:
    def __init__(
        self,
        output_dir: Path,
        *,
        pdf_filename: str,
        pdf_sha256: str,
        enabled_analysts: Sequence[str],
    ) -> None:
        self.output_dir = output_dir
        self.pdf_filename = pdf_filename
        self.pdf_sha256 = pdf_sha256
        self.enabled_analysts = list(enabled_analysts)
        self.manifest_path = output_dir / CHECKPOINT_FILENAME

    def load_compatible_reports(
        self,
        identities: list[AnalystIdentity],
        *,
        resume: bool,
        legacy_context: LegacyRunContext | None = None,
        adopt_legacy: bool = True,
        allow_manifest_updates: bool | None = None,
    ) -> dict[str, str]:
        if not resume:
            return {}
        if not adopt_legacy:
            allow_manifest_updates = False
        elif allow_manifest_updates is None:
            allow_manifest_updates = True

        manifest = self._read_manifest()
        if manifest is not None:
            reports = self._reports_from_manifest(manifest, identities)
            expected_keys = {identity.key for identity in identities}
            if (
                set(reports) == expected_keys
                and manifest.get("enabled_analysts") != self.enabled_analysts
                and allow_manifest_updates
            ):
                self._write_manifest(manifest)
            return reports

        if self.manifest_path.exists() or legacy_context is None or not adopt_legacy:
            return {}
        if not self._legacy_context_matches(legacy_context):
            return {}

        reports = self._valid_legacy_reports(identities)
        if adopt_legacy and allow_manifest_updates:
            for identity in identities:
                if identity.key in reports:
                    self._mark_completed(identity, attempt=0, counters=None)
        return reports

    def record_running(self, identity: AnalystIdentity, *, attempt: int) -> None:
        manifest = self._read_manifest() or self._new_manifest()
        entry = self._entry(manifest, identity.key)
        entry.update(
            {
                "status": "running",
                "total_attempts": int(entry.get("total_attempts", 0)) + 1,
                "attempts_in_last_invocation": attempt,
                "updated_at": _utc_now_isoformat(),
            }
        )
        self._write_manifest(manifest)

    def record_failure(
        self,
        identity: AnalystIdentity,
        *,
        attempt: int,
        failure_kind: str,
        error: str,
        execution_diagnostic: SafeExecutionDiagnostic | None = None,
        validation_diagnostic: SafeValidationDiagnostic | None = None,
        counters: RetryCounters | None = None,
    ) -> None:
        """Record a rejected or failed attempt in the manifest.

        The rejected reply itself is not written here: ``run_analysts`` keeps it
        under ``attempts/<key>.<n>.invalid.json`` or ``.invalid.txt``, which is
        the scheme the spec mandates. Only the sanitized diagnostic is persisted
        here, so the manifest never carries model- or provider-controlled text.
        """
        failure_kind = validate_failure_kind(failure_kind)
        # An execution or terminal failure is a provider error and carries
        # execution metadata; a validation or truncation failure is about the
        # reply and carries validation issues.
        if failure_kind in ("execution", "terminal"):
            if validation_diagnostic is not None:
                raise ValueError(
                    f"{failure_kind} failures cannot carry validation diagnostics"
                )
            error = render_execution_diagnostic(execution_diagnostic)
        else:
            if execution_diagnostic is not None:
                raise ValueError(
                    f"{failure_kind} failures cannot carry execution diagnostics"
                )
            default_code = (
                "report_truncated" if failure_kind == "truncation" else "invalid_report"
            )
            source = validation_diagnostic or SafeValidationDiagnostic(
                (("report", default_code),)
            )
            error = render_validation_diagnostic(validate_validation_diagnostic(source))
        manifest = self._read_manifest() or self._new_manifest()
        entry = self._entry(manifest, identity.key)
        failed_at = _utc_now_isoformat()
        total_attempt = int(entry.get("total_attempts", 0))
        history = entry.get("attempt_history")
        if not isinstance(history, list):
            history = []
            entry["attempt_history"] = history
        history.append(
            {
                "invocation_attempt": attempt,
                "total_attempt": total_attempt,
                "failure_kind": failure_kind,
                "error": error,
                "failed_at": failed_at,
            }
        )
        entry.update(
            {
                "status": "failed",
                "attempts_in_last_invocation": attempt,
                "failure_kind": failure_kind,
                "last_error": error,
                "failed_at": failed_at,
            }
        )
        if counters is not None:
            entry.update(_counter_fields(counters))
        self._write_manifest(manifest)

    def record_completed(
        self,
        identity: AnalystIdentity,
        *,
        attempt: int,
        report_text: str,
        assessment_json: str,
        counters: RetryCounters,
    ) -> None:
        """Persist the report and its validated-assessment sidecar, then mark done.

        Both are validated before anything is written, and the manifest entry is
        written last: a crash between the files leaves the analyst not
        completed rather than half-recorded.
        """
        validate_analyst_report(report_text)
        _validate_assessment_text(assessment_json)
        atomic_write_text(self.output_dir / identity.assessment_filename, assessment_json)
        atomic_write_text(self.output_dir / identity.report_filename, report_text)
        self._mark_completed(identity, attempt=attempt, counters=counters)

    def _mark_completed(
        self,
        identity: AnalystIdentity,
        *,
        attempt: int,
        counters: RetryCounters | None,
    ) -> None:
        # Counter keys may be absent (legacy adoption passes counters=None and
        # invents none) or carried over from an earlier attempt (record_failure
        # without counters leaves what a previous call wrote). A consumer must
        # read them with .get and must not attribute a present value to the
        # current attempt.
        manifest = self._read_manifest() or self._new_manifest()
        entry = self._entry(manifest, identity.key)
        entry.pop("failure_kind", None)
        entry.pop("failed_at", None)
        entry.update(
            {
                "status": "completed",
                "attempts_in_last_invocation": attempt,
                "report_file": identity.report_filename,
                "assessment_file": identity.assessment_filename,
                "fingerprint": identity.fingerprint,
                "completed_at": _utc_now_isoformat(),
                "last_error": None,
            }
        )
        if counters is not None:
            entry.update(_counter_fields(counters))
        self._write_manifest(manifest)

    def all_completed(
        self,
        identities: list[AnalystIdentity],
        *,
        legacy_context: LegacyRunContext | None = None,
    ) -> bool:
        reports = self.load_compatible_reports(
            identities,
            resume=True,
            legacy_context=legacy_context,
            adopt_legacy=False,
            allow_manifest_updates=False,
        )
        return len(reports) == len(identities)

    def _new_manifest(self) -> dict[str, Any]:
        return {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "pdf_filename": self.pdf_filename,
            "pdf_sha256": self.pdf_sha256,
            "enabled_analysts": self.enabled_analysts,
            "analysts": {},
        }

    def _read_manifest(self) -> dict[str, Any] | None:
        if not self.manifest_path.exists():
            return None
        try:
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(manifest, dict):
            return None
        if manifest.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
            return None
        if manifest.get("pdf_filename") != self.pdf_filename:
            return None
        if manifest.get("pdf_sha256") != self.pdf_sha256:
            return None
        if not isinstance(manifest.get("analysts"), dict):
            return None
        return manifest

    def _reports_from_manifest(
        self, manifest: dict[str, Any], identities: list[AnalystIdentity]
    ) -> dict[str, str]:
        analyst_entries = manifest["analysts"]
        reports: dict[str, str] = {}
        for identity in identities:
            entry = analyst_entries.get(identity.key)
            if not isinstance(entry, dict):
                continue
            if entry.get("status") != "completed":
                continue
            if entry.get("fingerprint") != identity.fingerprint:
                continue
            if entry.get("report_file") != identity.report_filename:
                continue
            if entry.get("assessment_file") != identity.assessment_filename:
                continue
            report_text = self._read_complete_artifacts(identity)
            if report_text is not None:
                reports[identity.key] = report_text
        return reports

    def _legacy_context_matches(self, context: LegacyRunContext) -> bool:
        if context.pdf_filename != self.pdf_filename:
            return False
        run_status_path = self.output_dir / "run_status.json"
        if not run_status_path.exists():
            return True
        try:
            run_status = json.loads(run_status_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False
        return (
            isinstance(run_status, dict)
            and run_status.get("pdf_filename") == context.pdf_filename
            and run_status.get("masking_enabled") == context.masking_enabled
            and run_status.get("strict_scoring") == context.strict_scoring
        )

    def _valid_legacy_reports(
        self, identities: list[AnalystIdentity]
    ) -> dict[str, str]:
        reports: dict[str, str] = {}
        for identity in identities:
            report_text = self._read_complete_artifacts(identity)
            if report_text is not None:
                reports[identity.key] = report_text
        return reports

    def _read_complete_artifacts(self, identity: AnalystIdentity) -> str | None:
        """The report text, only when the report AND its sidecar both validate.

        A report without its assessment would resume an analyst whose audit
        fields were never kept, so it counts as not completed.
        """
        report_text = self._read_valid_report(self.output_dir / identity.report_filename)
        if report_text is None:
            return None
        try:
            _validate_assessment_text(
                (self.output_dir / identity.assessment_filename).read_text(
                    encoding="utf-8"
                )
            )
        except (OSError, UnicodeError, ValueError):
            return None
        return report_text

    @staticmethod
    def _read_valid_report(path: Path) -> str | None:
        try:
            report_text = path.read_text(encoding="utf-8")
            validate_analyst_report(report_text)
        except (OSError, UnicodeError, ValueError):
            return None
        return report_text

    @staticmethod
    def _entry(manifest: dict[str, Any], key: str) -> dict[str, Any]:
        analysts = manifest["analysts"]
        entry = analysts.get(key)
        if not isinstance(entry, dict):
            entry = {}
            analysts[key] = entry
        return entry

    def _write_manifest(self, manifest: dict[str, Any]) -> None:
        manifest.update(
            {
                "schema_version": CHECKPOINT_SCHEMA_VERSION,
                "pdf_filename": self.pdf_filename,
                "pdf_sha256": self.pdf_sha256,
                "enabled_analysts": self.enabled_analysts,
            }
        )
        atomic_write_json(self.manifest_path, manifest)


def _validate_assessment_text(assessment_json: object) -> None:
    """Raise ValueError unless this is the JSON text of a valid RucamAssessment."""
    if type(assessment_json) is not str:
        raise TypeError("assessment_json must be a string")
    try:
        RucamAssessment.model_validate_json(assessment_json)
    except ValidationError:
        # A ValidationError is a ValueError, but its message quotes the input.
        raise ValueError("assessment_json is not a valid RUCAM assessment") from None


def _counter_fields(counters: RetryCounters) -> dict[str, Any]:
    return {
        "validation_retries": counters.validation_retries,
        "transient_retries": counters.transient_retries,
        "truncation_escalations": counters.truncation_escalations,
        "mode_downgrades": counters.mode_downgrades,
        "final_mode": counters.final_mode.value,
    }


def _utc_now_isoformat() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "AnalystCheckpointStore",
    "AnalystIdentity",
    "CHECKPOINT_FILENAME",
    "CHECKPOINT_SCHEMA_VERSION",
    "LegacyRunContext",
    "atomic_write_json",
    "atomic_write_text",
    "build_analyst_fingerprint",
    "assessment_filename_for",
    "build_analyst_identities",
]
