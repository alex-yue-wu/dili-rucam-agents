from __future__ import annotations

import argparse
from pathlib import Path
from typing import Optional

from dili_rucam_agents.crew.crew import run_crew
from dili_rucam_agents.masking import (
    MASK_TOKEN,
    extract_patient_specific_rucam_scores,
    is_patient_specific_outcome_line,
    parse_case_bundle_json,
)


def run_end_to_end(
    pdf_path: str,
    prompt_path: Optional[str] = None,
    output_dir: Optional[str] = None,
    *,
    enable_score_masking: bool = False,
    strict_scoring: bool = False,
    use_analyst_delta: bool = False,
    use_analyst_epsilon: bool = False,
    use_analyst_zeta: bool = False,
    use_analyst_eta: bool = False,
) -> str:
    """Public helper used by scripts/tests to run the full pipeline."""

    resolved_pdf = str(Path(pdf_path).expanduser().resolve())
    resolved_prompt = Path(prompt_path).expanduser().resolve() if prompt_path else None
    resolved_output_dir = Path(output_dir).expanduser().resolve() if output_dir else None

    if resolved_output_dir:
        resolved_output_dir.mkdir(parents=True, exist_ok=True)

    result = run_crew(
        resolved_pdf,
        resolved_prompt,
        capture_reports=resolved_output_dir is not None,
        enable_score_masking=enable_score_masking,
        strict_scoring=strict_scoring,
        use_analyst_delta=use_analyst_delta,
        use_analyst_epsilon=use_analyst_epsilon,
        use_analyst_zeta=use_analyst_zeta,
        use_analyst_eta=use_analyst_eta,
    )

    if isinstance(result, tuple):
        final_output, reports = result
    else:
        final_output, reports = result, {}

    if resolved_output_dir and isinstance(reports, dict):
        _persist_reports(reports, resolved_output_dir)

    return final_output


def _main() -> None:
    parser = argparse.ArgumentParser(description="Execute the full RUCAM crew on a PDF.")
    parser.add_argument("pdf_path", help="Path to the clinical case report PDF.")
    parser.add_argument(
        "--prompt-path",
        dest="prompt_path",
        help="Optional override for the production prompt file.",
    )
    parser.add_argument(
        "--output-dir",
        dest="output_dir",
        help="Directory to store markdown reports for enabled analysts.",
    )
    parser.add_argument(
        "--mask-scores",
        dest="enable_score_masking",
        action="store_true",
        help="Enable the optional score masking agent between ingestion and analysts.",
    )
    parser.add_argument(
        "--strict-scoring",
        dest="strict_scoring",
        action="store_true",
        help="Use the strict scoring prompt instead of the default inferring prompt.",
    )
    parser.add_argument(
        "--analyst-delta",
        dest="use_analyst_delta",
        action="store_true",
        help="Enable the optional Analyst Delta agent (ANALYST_DELTA_MODEL).",
    )
    parser.add_argument(
        "--analyst-epsilon",
        dest="use_analyst_epsilon",
        action="store_true",
        help="Enable the optional Analyst Epsilon agent (ANALYST_EPSILON_MODEL).",
    )
    parser.add_argument(
        "--analyst-zeta",
        dest="use_analyst_zeta",
        action="store_true",
        help="Enable the optional Analyst Zeta agent (ANALYST_ZETA_MODEL).",
    )
    parser.add_argument(
        "--analyst-eta",
        dest="use_analyst_eta",
        action="store_true",
        help="Enable the optional Analyst Eta agent (ANALYST_ETA_MODEL).",
    )
    args = parser.parse_args()
    print(
        run_end_to_end(
            args.pdf_path,
            args.prompt_path,
            args.output_dir,
            enable_score_masking=args.enable_score_masking,
            strict_scoring=args.strict_scoring,
            use_analyst_delta=args.use_analyst_delta,
            use_analyst_epsilon=args.use_analyst_epsilon,
            use_analyst_zeta=args.use_analyst_zeta,
            use_analyst_eta=args.use_analyst_eta,
        )
    )


def _persist_reports(reports: dict[str, Optional[str]], output_dir: Path) -> None:
    has_masking_outputs = bool(reports.get("raw_case_bundle") and reports.get("masked_case_bundle"))
    filename_map = {
        "masked_case_bundle": "masked-case-bundle_report.md",
        "ground_truth_rucam_score": "ground-truth-rucam-score_report.md",
        "analyst_alpha": "analyst-alpha_report.md",
        "analyst_beta": "analyst-beta_report.md",
        "analyst_gamma": "analyst-gamma_report.md",
        "analyst_delta": "analyst-delta_report.md",
        "analyst_epsilon": "analyst-epsilon_report.md",
        "analyst_zeta": "analyst-zeta_report.md",
        "analyst_eta": "analyst-eta_report.md",
        "gpt_52": "gpt-5.2_report.md",
        "gemini_30": "gemini-3.0_report.md",
    }

    for key, content in reports.items():
        if not content:
            continue
        if key in {"masked_case_bundle", "ground_truth_rucam_score"} and not has_masking_outputs:
            continue

        if key in filename_map:
            filename = filename_map[key]
        else:
            continue

        if key == "masked_case_bundle":
            content = _render_masked_case_bundle_report(
                raw_case_bundle_payload=reports.get("raw_case_bundle"),
                masked_case_bundle_payload=content,
            )
        (output_dir / filename).write_text(content, encoding="utf-8")


def _render_masked_case_bundle_report(
    *,
    raw_case_bundle_payload: Optional[str],
    masked_case_bundle_payload: str,
) -> str:
    masked_payload = parse_case_bundle_json(masked_case_bundle_payload)
    raw_payload = parse_case_bundle_json(raw_case_bundle_payload) if raw_case_bundle_payload else None
    extraction_notes = masked_payload.get("extraction_notes", [])
    masked_text_pairs = _extract_masked_text_pairs(
        raw_payload.get("normalized_text", "") if raw_payload else "",
        masked_payload.get("normalized_text", ""),
    )
    masked_table_previews = _extract_masked_table_preview_pairs(
        raw_payload.get("tables", []) if raw_payload else [],
        masked_payload.get("tables", []),
    )
    masked_rucam_scores = _extract_masked_rucam_scores_from_payloads(raw_payload, masked_payload)
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
