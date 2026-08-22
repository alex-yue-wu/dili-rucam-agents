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

from dili_rucam_agents.crew.agents import (
    resolve_analyst_max_output_tokens,
    resolve_rucam_model,
)
from dili_rucam_agents.validators.analyst_report import validate_analyst_report


CHECKPOINT_SCHEMA_VERSION = 2
CHECKPOINT_FILENAME = "analyst_checkpoints.json"


@dataclass(frozen=True)
class AnalystIdentity:
    key: str
    report_filename: str
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
) -> str:
    payload = {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "pdf_sha256": pdf_sha256,
        "analyst_instruction_sha256": analyst_instruction_sha256,
        "enable_score_masking": enable_score_masking,
        "strict_scoring": strict_scoring,
        "model": model,
        "max_output_tokens": max_output_tokens,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def build_analyst_identities(
    *,
    configs: list[dict[str, Any]],
    report_filename_map: Mapping[str, str],
    pdf_sha256: str,
    analyst_instruction_sha256: Mapping[str, str],
    enable_score_masking: bool,
    strict_scoring: bool,
) -> dict[str, AnalystIdentity]:
    identities: dict[str, AnalystIdentity] = {}
    for config in configs:
        key = config["key"]
        model = resolve_rucam_model(
            model_env=config["model_env"],
            fallback_envs=tuple(config["fallback_envs"]),
            default_model=config["default_model"],
        )
        max_output_tokens = resolve_analyst_max_output_tokens(config["max_tokens_env"])
        identities[key] = AnalystIdentity(
            key=key,
            report_filename=report_filename_map[key],
            fingerprint=build_analyst_fingerprint(
                pdf_sha256=pdf_sha256,
                analyst_instruction_sha256=analyst_instruction_sha256[key],
                enable_score_masking=enable_score_masking,
                strict_scoring=strict_scoring,
                model=model,
                max_output_tokens=max_output_tokens,
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
    ) -> dict[str, str]:
        if not resume:
            return {}

        manifest = self._read_manifest()
        if manifest is not None:
            reports = self._reports_from_manifest(manifest, identities)
            expected_keys = {identity.key for identity in identities}
            if (
                set(reports) == expected_keys
                and manifest.get("enabled_analysts") != self.enabled_analysts
            ):
                self._write_manifest(manifest)
            return reports

        if self.manifest_path.exists() or legacy_context is None:
            return {}
        if not self._legacy_context_matches(legacy_context):
            return {}

        reports = self._valid_legacy_reports(identities)
        if adopt_legacy:
            for identity in identities:
                report_text = reports.get(identity.key)
                if report_text is not None:
                    self.record_completed(identity, attempt=0, report_text=report_text)
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
        report_text: str | None = None,
    ) -> None:
        manifest = self._read_manifest() or self._new_manifest()
        entry = self._entry(manifest, identity.key)
        failed_at = _utc_now_isoformat()
        total_attempt = int(entry.get("total_attempts", 0))
        artifact_file = None
        if report_text is not None:
            artifact_file = (
                "attempts/"
                f"{identity.key.replace('_', '-')}_attempt-{total_attempt:06d}-"
                f"{uuid4().hex}.invalid.md"
            )
            atomic_write_text(self.output_dir / artifact_file, report_text)
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
                "artifact_file": artifact_file,
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
        self._write_manifest(manifest)

    def record_completed(
        self, identity: AnalystIdentity, *, attempt: int, report_text: str
    ) -> None:
        validate_analyst_report(report_text)
        atomic_write_text(self.output_dir / identity.report_filename, report_text)

        manifest = self._read_manifest() or self._new_manifest()
        entry = self._entry(manifest, identity.key)
        entry.update(
            {
                "status": "completed",
                "attempts_in_last_invocation": attempt,
                "report_file": identity.report_filename,
                "fingerprint": identity.fingerprint,
                "completed_at": _utc_now_isoformat(),
                "last_error": None,
            }
        )
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
            report_text = self._read_valid_report(
                self.output_dir / identity.report_filename
            )
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
            report_text = self._read_valid_report(
                self.output_dir / identity.report_filename
            )
            if report_text is not None:
                reports[identity.key] = report_text
        return reports

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
    "build_analyst_identities",
]
