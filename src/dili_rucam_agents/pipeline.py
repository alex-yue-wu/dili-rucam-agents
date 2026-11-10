"""The end-to-end run: ingest a PDF, resolve item 6, mask, score, persist.

Who scores is declared in a roster file rather than switched on by flags, so
the number of analysts is whatever that file lists. Everything above the
provider layer is deterministic: the bundle, the evidence cascade, the masking
and the instruction contracts are all built before the first call.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from functools import partial
import hashlib
import json
from pathlib import Path
from typing import Optional

from dotenv import find_dotenv, load_dotenv

from dili_rucam_agents.analysts import AnalystAttemptEvent, run_analysts
from dili_rucam_agents.checkpoints import (
    AnalystCheckpointStore,
    AnalystIdentity,
    LegacyRunContext,
    atomic_write_text,
    build_analyst_identities,
)
from dili_rucam_agents.contracts import (
    AnalystInstructionContract,
    build_analyst_instruction_contract,
    load_rucam_prompt,
)
from dili_rucam_agents.diagnostics import (
    SafeValidationDiagnostic,
    validate_validation_diagnostic,
)
from dili_rucam_agents.ground_truth import run_ground_truth_score_finder
from dili_rucam_agents.hepatotoxicity.evidence import DEFAULT_EVIDENCE_AS_OF
from dili_rucam_agents.hepatotoxicity.report import render_hepatotoxicity_report
from dili_rucam_agents.hepatotoxicity.settings import (
    HepatotoxicityEvidenceSettings,
    apply_hepatotoxicity_evidence,
)
from dili_rucam_agents.ingestion.build_bundle import build_case_bundle
from dili_rucam_agents.masking import (
    MASK_TOKEN,
    extract_patient_specific_rucam_scores,
    is_patient_specific_outcome_line,
    mask_case_bundle_payload,
    parse_case_bundle_json,
)
from dili_rucam_agents.providers import CompletionProvider, SdkProvider, require_api_keys
from dili_rucam_agents.roster import AnalystRoster, load_roster
from dili_rucam_agents.runner import RetryCounters, validate_max_restarts

#: The reports that are not an analyst's. Analyst filenames are not listed here:
#: they come from ``AnalystConfig.report_filename``, so adding a roster entry
#: adds its report without editing this module.
_REPORT_FILENAME_MAP = {
    "hepatotoxicity_evidence": "hepatotoxicity-evidence_report.md",
    "masked_case_bundle": "masked-case-bundle_report.md",
    "ground_truth_rucam_score": "ground-truth-rucam-score_report.md",
}

_INVALID_REPORT = SafeValidationDiagnostic((("report", "invalid_report"),))


def _build_provider() -> CompletionProvider:
    """One transport per call site.

    ``run_analysts`` and ``run_ground_truth_score_finder`` each own an
    ``asyncio.run``, so a client built inside one loop must not be handed to the
    next; a provider per call site keeps every client inside one loop. Tests
    replace this function with one that returns a fake.
    """
    return SdkProvider()


@dataclass(frozen=True)
class PipelineCheckpointContext:
    store: AnalystCheckpointStore
    identities: dict[str, AnalystIdentity]
    instruction_contracts: dict[str, AnalystInstructionContract]
    legacy_context: LegacyRunContext
    #: Written by the "completed" attempt event and read by the ``on_report``
    #: call that follows it for the same analyst. ``run_analysts`` emits the
    #: event first and the report second, and the event is the only carrier of
    #: the attempt number and the retry counters ``record_completed`` needs.
    completions: dict[str, tuple[int, RetryCounters]] = field(default_factory=dict)


def _build_instruction_contracts(
    *,
    roster: AnalystRoster,
    prompt_text: str,
    bundle_input_name: str,
) -> dict[str, AnalystInstructionContract]:
    """One contract per roster analyst, built once and both hashed and sent."""
    return {
        config.key: build_analyst_instruction_contract(
            analyst_label=config.label,
            prompt_text=prompt_text,
            model_name=config.model,
            bundle_input_name=bundle_input_name,
        )
        for config in roster.analysts
    }


def _bundle_input_name(enable_score_masking: bool) -> str:
    return "masked_case_bundle_json" if enable_score_masking else "raw_case_bundle_json"


def _build_checkpoint_context(
    *,
    pdf_path: Path,
    output_dir: Path,
    roster: AnalystRoster,
    instruction_contracts: dict[str, AnalystInstructionContract],
    enable_score_masking: bool,
    strict_scoring: bool,
    hepatotoxicity_settings: HepatotoxicityEvidenceSettings,
) -> PipelineCheckpointContext:
    pdf_sha256 = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    identities = build_analyst_identities(
        configs=list(roster.analysts),
        report_filename_map={
            config.key: config.report_filename for config in roster.analysts
        },
        pdf_sha256=pdf_sha256,
        analyst_instruction_sha256={
            key: contract.sha256 for key, contract in instruction_contracts.items()
        },
        enable_score_masking=enable_score_masking,
        strict_scoring=strict_scoring,
        hepatotoxicity_evidence_sha256=hepatotoxicity_settings.fingerprint(),
    )
    return PipelineCheckpointContext(
        store=AnalystCheckpointStore(
            output_dir,
            pdf_filename=pdf_path.name,
            pdf_sha256=pdf_sha256,
            enabled_analysts=tuple(identities),
        ),
        identities=identities,
        instruction_contracts=instruction_contracts,
        legacy_context=LegacyRunContext(
            pdf_filename=pdf_path.name,
            masking_enabled=enable_score_masking,
            strict_scoring=strict_scoring,
        ),
    )


def _failure_kind_for(event: AnalystAttemptEvent) -> str:
    """Which failure the manifest records for a rejected attempt.

    A cut-off reply and a reply that broke the schema arrive as the same event
    status, so the diagnostic decides. It is revalidated before anything reads
    its issues, since the store refuses a forged one anyway.
    """
    if event.status == "execution_failed":
        # Both an exhausted transient budget and a terminal status code reach
        # the pipeline as this one status; the execution diagnostic carries the
        # status code that tells them apart.
        return "execution"
    diagnostic = validate_validation_diagnostic(
        event.validation_diagnostic or _INVALID_REPORT
    )
    truncated = any(code == "report_truncated" for _field, code in diagnostic.issues)
    return "truncation" if truncated else "validation"


def _handle_attempt_event(
    context: PipelineCheckpointContext,
    event: AnalystAttemptEvent,
) -> None:
    identity = context.identities[event.analyst_key]
    if event.status == "running":
        context.store.record_running(identity, attempt=event.attempt)
        return
    if event.status in ("validation_failed", "execution_failed"):
        context.store.record_failure(
            identity,
            attempt=event.attempt,
            failure_kind=_failure_kind_for(event),
            # Ignored: the store recomputes the message from the diagnostic so
            # no caller-supplied text can reach the manifest.
            error="",
            execution_diagnostic=event.execution_diagnostic,
            validation_diagnostic=event.validation_diagnostic,
            counters=event.counters,
        )
        return
    if event.status == "completed":
        if event.counters is None:
            raise ValueError("completed analyst attempt is missing retry counters")
        context.completions[event.analyst_key] = (event.attempt, event.counters)
        return
    raise ValueError(f"Unsupported analyst attempt status: {event.status}")


def _persist_completed_analyst(
    context: PipelineCheckpointContext,
    analyst_key: str,
    report_text: str,
    assessment: dict,
) -> None:
    """Hand both artifacts to the store, which validates and writes them."""
    completion = context.completions.get(analyst_key)
    if completion is None:
        raise ValueError(
            f"{analyst_key} reported a completed report without a completed attempt"
        )
    attempt, counters = completion
    context.store.record_completed(
        context.identities[analyst_key],
        attempt=attempt,
        report_text=report_text,
        assessment_json=json.dumps(assessment, indent=2, sort_keys=True) + "\n",
        counters=counters,
    )


def run_end_to_end(
    pdf_path: str,
    prompt_path: Optional[str] = None,
    output_dir: Optional[str] = None,
    *,
    roster_path: str | None = None,
    enable_score_masking: bool = False,
    strict_scoring: bool = False,
    max_restarts: int = 2,
    resume: bool = True,
    hepatotoxicity_settings: HepatotoxicityEvidenceSettings | None = None,
) -> str:
    """Score one PDF with every analyst the roster declares.

    Returns the last roster analyst's report, or an empty string when no
    analyst produced one. Reports and their assessment sidecars are persisted
    as each analyst finishes, so a failure part-way through keeps the work
    already paid for.
    """

    max_restarts = validate_max_restarts(max_restarts)
    evidence_settings = hepatotoxicity_settings or HepatotoxicityEvidenceSettings()
    resolved_pdf = Path(pdf_path).expanduser().resolve()
    resolved_prompt = Path(prompt_path).expanduser().resolve() if prompt_path else None
    resolved_output_dir = (
        Path(output_dir).expanduser().resolve() if output_dir else None
    )
    roster = load_roster(roster_path)

    # Before ingestion: the ingestion stack and the item 6 cascade are the
    # expensive part of a run and a missing key is knowable without them. The
    # ground-truth model is only called when masking is on.
    needed_providers = {config.provider for config in roster.analysts}
    if enable_score_masking:
        needed_providers.add(roster.ground_truth.provider)
    require_api_keys(needed_providers)

    if resolved_output_dir:
        resolved_output_dir.mkdir(parents=True, exist_ok=True)

    prompt_text = load_rucam_prompt(resolved_prompt, strict_scoring=strict_scoring)
    bundle_input_name = _bundle_input_name(enable_score_masking)
    instruction_contracts = _build_instruction_contracts(
        roster=roster,
        prompt_text=prompt_text,
        bundle_input_name=bundle_input_name,
    )

    checkpoint_context = (
        _build_checkpoint_context(
            pdf_path=resolved_pdf,
            output_dir=resolved_output_dir,
            roster=roster,
            instruction_contracts=instruction_contracts,
            enable_score_masking=enable_score_masking,
            strict_scoring=strict_scoring,
            hepatotoxicity_settings=evidence_settings,
        )
        if resolved_output_dir
        else None
    )
    completed_reports = (
        checkpoint_context.store.load_compatible_reports(
            list(checkpoint_context.identities.values()),
            resume=resume,
            legacy_context=checkpoint_context.legacy_context,
        )
        if checkpoint_context
        else {}
    )

    (
        raw_case_bundle_json,
        masked_case_bundle_json,
        evidence_markdown,
    ) = _prepare_case_bundle_json(
        pdf_path=resolved_pdf,
        enable_score_masking=enable_score_masking,
        hepatotoxicity_settings=evidence_settings,
    )

    # Both callbacks persist through the store, so neither exists without one.
    on_attempt = on_report = None
    if checkpoint_context is not None:
        on_attempt = partial(_handle_attempt_event, checkpoint_context)
        on_report = partial(_persist_completed_analyst, checkpoint_context)

    outcome = run_analysts(
        case_bundle_json=masked_case_bundle_json or raw_case_bundle_json,
        roster=roster,
        prompt_text=prompt_text,
        bundle_input_name=bundle_input_name,
        provider=_build_provider(),
        max_retries=max_restarts,
        completed_reports=completed_reports,
        on_report=on_report,
        on_attempt=on_attempt,
        output_dir=resolved_output_dir,
        instruction_contracts=instruction_contracts,
    )

    if resolved_output_dir:
        reports: dict[str, Optional[str]] = {}
        if masked_case_bundle_json:
            reports["raw_case_bundle"] = raw_case_bundle_json
            reports["masked_case_bundle"] = masked_case_bundle_json
            reports["ground_truth_rucam_score"] = run_ground_truth_score_finder(
                raw_case_bundle_json=raw_case_bundle_json,
                config=roster.ground_truth,
                provider=_build_provider(),
            )
        if evidence_markdown:
            reports["hepatotoxicity_evidence"] = evidence_markdown
        _persist_reports(reports, resolved_output_dir)

    return next(reversed(outcome.reports.values()), "") or ""


def is_end_to_end_complete(
    pdf_path: str,
    output_dir: str,
    prompt_path: str | None = None,
    *,
    roster_path: str | None = None,
    enable_score_masking: bool = False,
    strict_scoring: bool = False,
    hepatotoxicity_settings: HepatotoxicityEvidenceSettings | None = None,
) -> bool:
    """Return whether every roster analyst's checkpoint is compatible and valid."""

    resolved_pdf = Path(pdf_path).expanduser().resolve()
    resolved_output_dir = Path(output_dir).expanduser().resolve()
    resolved_prompt = Path(prompt_path).expanduser().resolve() if prompt_path else None
    roster = load_roster(roster_path)
    prompt_text = load_rucam_prompt(resolved_prompt, strict_scoring=strict_scoring)
    context = _build_checkpoint_context(
        pdf_path=resolved_pdf,
        output_dir=resolved_output_dir,
        roster=roster,
        instruction_contracts=_build_instruction_contracts(
            roster=roster,
            prompt_text=prompt_text,
            bundle_input_name=_bundle_input_name(enable_score_masking),
        ),
        enable_score_masking=enable_score_masking,
        strict_scoring=strict_scoring,
        hepatotoxicity_settings=(
            hepatotoxicity_settings or HepatotoxicityEvidenceSettings()
        ),
    )
    reports = context.store.load_compatible_reports(
        list(context.identities.values()),
        resume=True,
        legacy_context=context.legacy_context,
        adopt_legacy=False,
        allow_manifest_updates=False,
    )
    if set(reports) != set(context.identities):
        return False
    if enable_score_masking:
        return all(
            (resolved_output_dir / _REPORT_FILENAME_MAP[key]).exists()
            for key in ("masked_case_bundle", "ground_truth_rucam_score")
        )
    return True


def _prepare_case_bundle_json(
    *,
    pdf_path: Path,
    enable_score_masking: bool,
    hepatotoxicity_settings: HepatotoxicityEvidenceSettings | None = None,
) -> tuple[str, Optional[str], Optional[str]]:
    """Build the bundle, resolve item 6 evidence into it, then optionally mask.

    The evidence block is attached before masking so that the raw bundle, the
    masked bundle and the persisted evidence report all describe one resolution.
    Masking rewrites only the extracted text surfaces, never this block.
    """

    raw_payload = build_case_bundle(pdf_path).to_dict()
    settings = hepatotoxicity_settings or HepatotoxicityEvidenceSettings()
    raw_payload, resolution = apply_hepatotoxicity_evidence(raw_payload, settings)
    evidence_markdown = (
        render_hepatotoxicity_report(resolution) if resolution is not None else None
    )
    raw_case_bundle_json = json.dumps(raw_payload, indent=2)

    if not enable_score_masking:
        return raw_case_bundle_json, None, evidence_markdown

    masked_payload = mask_case_bundle_payload(raw_payload)
    return (
        raw_case_bundle_json,
        json.dumps(masked_payload, indent=2),
        evidence_markdown,
    )


def _main() -> None:
    # Keys may live in a .env file: the agent framework this tier replaced
    # loaded one as an import side effect, and python-dotenv is a declared
    # dependency, so a run started from the command line still reads it.
    # Called here rather than at import time, so a library caller keeps control
    # of its own environment. Values already exported win, which is
    # load_dotenv's default. usecwd searches upward from the directory the
    # command was run in, rather than from this file, so the .env a user sees
    # beside their work is the one that is read; find_dotenv returns "" when
    # there is none and load_dotenv("") is a no-op.
    load_dotenv(find_dotenv(usecwd=True))
    parser = argparse.ArgumentParser(
        description="Score a clinical case report PDF with the declared analyst roster."
    )
    parser.add_argument("pdf_path", help="Path to the clinical case report PDF.")
    parser.add_argument(
        "--analysts",
        dest="roster_path",
        metavar="PATH",
        help=(
            "Analyst roster JSON (default: config/analysts.json, one analyst). "
            "config/analysts.study.json declares the seven-analyst study roster."
        ),
    )
    parser.add_argument(
        "--prompt-path",
        dest="prompt_path",
        help="Optional override for the production prompt file.",
    )
    parser.add_argument(
        "--output-dir",
        dest="output_dir",
        help="Directory to store markdown reports for the roster's analysts.",
    )
    parser.add_argument(
        "--mask-scores",
        dest="enable_score_masking",
        action="store_true",
        help="Enable the optional score masking step between ingestion and analysts.",
    )
    parser.add_argument(
        "--strict-scoring",
        dest="strict_scoring",
        action="store_true",
        help="Use the strict scoring prompt instead of the default inferring prompt.",
    )
    parser.add_argument(
        "--no-hepatotoxicity-evidence",
        dest="hepatotoxicity_evidence",
        action="store_false",
        help=(
            "Do not resolve RUCAM item 6 evidence into the case bundle "
            "(analysts then score item 6 from the case text alone)."
        ),
    )
    parser.add_argument(
        "--hepatotoxicity-offline",
        dest="hepatotoxicity_network",
        action="store_false",
        help=(
            "Use only the pinned DILIrank workbook for item 6; "
            "do not query DailyMed or PubMed."
        ),
    )
    parser.add_argument(
        "--drug",
        dest="drug_names",
        action="append",
        default=[],
        metavar="NAME",
        help=(
            "Resolve item 6 evidence for this drug name even if DILIrank does "
            "not list it. Repeatable."
        ),
    )
    parser.add_argument(
        "--hepatotoxicity-cache",
        dest="hepatotoxicity_cache",
        help=(
            "JSON cache for DailyMed and PubMed answers; reused on later runs "
            "so a score never depends on the day it was computed."
        ),
    )
    parser.add_argument(
        "--dilirank-path",
        dest="dilirank_path",
        metavar="PATH",
        help=(
            "Override the DILIrank workbook location "
            "(default: dilirank_v2/DILIrank_v2.xlsx at the repository root)."
        ),
    )
    parser.add_argument(
        "--hepatotoxicity-as-of",
        dest="hepatotoxicity_as_of",
        default=DEFAULT_EVIDENCE_AS_OF,
        metavar="YYYY-MM-DD",
        help=(
            "Upper publication-date bound for the PubMed tier "
            f"(default: {DEFAULT_EVIDENCE_AS_OF})."
        ),
    )
    parser.add_argument(
        "--analyst-restarts",
        type=int,
        choices=range(0, 3),
        default=2,
        help="Number of restarts per incomplete analyst (0-2; default: 2).",
    )
    args = parser.parse_args()
    print(
        run_end_to_end(
            args.pdf_path,
            args.prompt_path,
            args.output_dir,
            roster_path=args.roster_path,
            enable_score_masking=args.enable_score_masking,
            strict_scoring=args.strict_scoring,
            max_restarts=args.analyst_restarts,
            hepatotoxicity_settings=HepatotoxicityEvidenceSettings(
                enabled=args.hepatotoxicity_evidence,
                drug_names=tuple(args.drug_names),
                network_enabled=args.hepatotoxicity_network,
                as_of=args.hepatotoxicity_as_of,
                cache_path=args.hepatotoxicity_cache,
                dilirank_path=args.dilirank_path,
            ),
        )
    )


def _persist_reports(reports: dict[str, Optional[str]], output_dir: Path) -> None:
    has_masking_outputs = bool(
        reports.get("raw_case_bundle") and reports.get("masked_case_bundle")
    )

    for key, content in reports.items():
        if not content:
            continue
        if (
            key in {"masked_case_bundle", "ground_truth_rucam_score"}
            and not has_masking_outputs
        ):
            continue

        if key in _REPORT_FILENAME_MAP:
            filename = _REPORT_FILENAME_MAP[key]
        else:
            continue

        if key == "masked_case_bundle":
            content = _render_masked_case_bundle_report(
                raw_case_bundle_payload=reports.get("raw_case_bundle"),
                masked_case_bundle_payload=content,
            )
        atomic_write_text(output_dir / filename, content)


def _render_masked_case_bundle_report(
    *,
    raw_case_bundle_payload: Optional[str],
    masked_case_bundle_payload: str,
) -> str:
    masked_payload = parse_case_bundle_json(masked_case_bundle_payload)
    raw_payload = (
        parse_case_bundle_json(raw_case_bundle_payload)
        if raw_case_bundle_payload
        else None
    )
    extraction_notes = masked_payload.get("extraction_notes", [])
    masked_text_pairs = _extract_masked_text_pairs(
        raw_payload.get("normalized_text", "") if raw_payload else "",
        masked_payload.get("normalized_text", ""),
    )
    masked_table_previews = _extract_masked_table_preview_pairs(
        raw_payload.get("tables", []) if raw_payload else [],
        masked_payload.get("tables", []),
    )
    masked_rucam_scores = _extract_masked_rucam_scores_from_payloads(
        raw_payload, masked_payload
    )
    sections = [
        "# Masked Case Bundle Report",
        "",
        "## Summary",
        f"- PDF Path: `{masked_payload.get('pdf_path', 'Unknown')}`",
        f"- Mask Token: `{MASK_TOKEN}`",
        f"- Masked Text Lines: `{len(masked_text_pairs)}`",
        f"- Masked Table Previews: `{len(masked_table_previews)}`",
        "",
        "## Masked RUCAM Scores",
        "```text",
        "\n".join(str(score) for score in masked_rucam_scores),
        "```",
        "",
        "## Stable Fields",
        "```text",
        f"MASKED_RUCAM_SCORES: {_format_stable_list(masked_rucam_scores)}",
        "```",
        "",
        "## Extraction Notes",
    ]

    if extraction_notes:
        sections.extend([f"- {note}" for note in extraction_notes])
    else:
        sections.append("- None")

    sections.extend(
        [
            "",
            "## Masked Text",
        ]
    )

    if masked_text_pairs:
        for index, pair in enumerate(masked_text_pairs, start=1):
            sections.extend(
                [
                    f"### Masked Text {index}",
                    "**Original**",
                    "```text",
                    pair["original"],
                    "```",
                    "**Masked**",
                    "```text",
                    pair["masked"],
                    "```",
                    "",
                ]
            )
    else:
        sections.extend(["No masked text found.", ""])

    sections.append("## Masked Table Previews")

    if masked_table_previews:
        for table in masked_table_previews:
            sections.extend(
                [
                    f"### Page {table['page_number']} Table {table['table_index']}",
                    "**Original**",
                    "```text",
                    table["original_preview"],
                    "```",
                    "**Masked**",
                    "```text",
                    table["masked_preview"],
                    "```",
                    "",
                ]
            )
    else:
        sections.append("No masked table previews found.")

    if sections[-1] == "":
        sections.pop()

    return "\n".join(sections) + "\n"


def _extract_masked_text_pairs(raw_text: str, masked_text: str) -> list[dict[str, str]]:
    raw_lines = raw_text.splitlines()
    masked_lines = masked_text.splitlines()
    pairs: list[dict[str, str]] = []

    for index, masked_line in enumerate(masked_lines):
        if MASK_TOKEN not in masked_line:
            continue
        original_line = raw_lines[index] if index < len(raw_lines) else ""
        pairs.append({"original": original_line, "masked": masked_line})

    return pairs


def _extract_masked_table_preview_pairs(
    raw_tables: list[dict],
    masked_tables: list[dict],
) -> list[dict[str, str | int]]:
    raw_table_map = {
        (table.get("page_number"), table.get("table_index")): table
        for table in raw_tables
    }
    pairs: list[dict[str, str | int]] = []

    for masked_table in masked_tables:
        masked_preview = masked_table.get("preview", "")
        if MASK_TOKEN not in masked_preview:
            continue
        key = (masked_table.get("page_number"), masked_table.get("table_index"))
        raw_table = raw_table_map.get(key, {})
        pairs.append(
            {
                "page_number": masked_table.get("page_number", "?"),
                "table_index": masked_table.get("table_index", "?"),
                "original_preview": raw_table.get("preview", ""),
                "masked_preview": masked_preview,
            }
        )

    return pairs


def _extract_masked_rucam_scores_from_payloads(
    raw_payload: Optional[dict],
    masked_payload: dict,
) -> list[int]:
    seen: set[int] = set()
    ordered_scores: list[int] = []
    if not raw_payload:
        return ordered_scores

    raw_lines = raw_payload.get("normalized_text", "").splitlines()
    masked_lines = masked_payload.get("normalized_text", "").splitlines()
    for index, masked_line in enumerate(masked_lines):
        if MASK_TOKEN not in masked_line:
            continue
        original_line = raw_lines[index] if index < len(raw_lines) else ""
        if not is_patient_specific_outcome_line(original_line):
            continue
        for score in extract_patient_specific_rucam_scores(original_line):
            if score in seen:
                continue
            seen.add(score)
            ordered_scores.append(score)

    return ordered_scores


def _format_stable_list(values: list[int] | list[str]) -> str:
    if not values:
        return "None"
    return ",".join(str(value) for value in values)


if __name__ == "__main__":  # pragma: no cover - CLI helper
    _main()
