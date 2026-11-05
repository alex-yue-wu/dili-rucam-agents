"""Markdown artifact for the item 6 evidence cascade.

The analysts read the evidence out of the case bundle; this report is the human
copy, written next to the analyst reports so a reviewer can see which source
supplied each item 6 score without opening the JSON.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .dailymed import DAILYMED_LABEL_URL
from .evidence import (
    EvidenceResolution,
    HepatotoxicityEvidence,
    TIER_DAILYMED,
    TIER_DILIRANK,
    TIER_LITERATURE,
    TIER_UNRESOLVED,
)
from .literature import PUBMED_QUERY_URL

REPORT_TITLE = "# Hepatotoxicity Evidence Report (RUCAM Item 6)"

_TIER_LABELS = {
    TIER_DILIRANK: "DILIrank v2",
    TIER_DAILYMED: "DailyMed label",
    TIER_LITERATURE: "PubMed case reports",
    TIER_UNRESOLVED: "Unresolved",
}


def _escape_cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def _score_cell(score: int | None) -> str:
    return "not resolved" if score is None else f"+{score}" if score > 0 else "0"


def _render_dilirank(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    lines = ["- DILIrank v2 rows:"]
    for row in rows:
        lines.append(
            f"  - {row.get('compound_name')} "
            f"({row.get('ltkb_id')}) -- {row.get('vdili_concern')}, "
            f"label section: {row.get('label_section') or 'not stated'}, "
            f"severity class: {row.get('severity_class')}, "
            f"item 6: {_score_cell(row.get('score'))}"
        )
    return lines


def _render_dailymed(payload: Mapping[str, Any]) -> list[str]:
    section = payload.get("section")
    searched = payload.get("searched_names") or []
    lines = [
        "- DailyMed:",
        f"  - label found: {'yes' if payload.get('label_found') else 'no'}",
        "  - names searched: "
        + (", ".join(str(name) for name in searched) or "none"),
        f"  - name that matched: {payload.get('search_name') or 'none'}",
        f"  - matching labels in DailyMed: {payload.get('total_labels', 0)}",
        f"  - strongest section mentioning liver injury: {section or 'none'}",
    ]
    terms = payload.get("matched_terms") or []
    if terms:
        lines.append("  - wording matched: " + ", ".join(str(term) for term in terms))
    labels = payload.get("labels") or []
    for label in labels:
        if not isinstance(label, Mapping):
            continue
        setid = str(label.get("setid") or "")
        lines.append(
            f"  - SPL {setid} v{label.get('spl_version')} "
            f"({label.get('published_date')}): {label.get('title')} -- "
            f"{DAILYMED_LABEL_URL.format(setid=setid)}"
        )
    return lines


def _render_literature(payload: Mapping[str, Any]) -> list[str]:
    query = str(payload.get("query") or "")
    lines = [
        "- PubMed:",
        f"  - case reports found: {payload.get('report_count', 0)} "
        f"(threshold {payload.get('min_reports', 1)})",
        f"  - as of: {payload.get('as_of')}",
    ]
    pmids = payload.get("pmids") or []
    if pmids:
        lines.append("  - sample PMIDs: " + ", ".join(str(pmid) for pmid in pmids))
    if query:
        lines.append(f"  - query: `{query}`")
        lines.append(
            "  - rerun: "
            + PUBMED_QUERY_URL.format(query=query.replace(" ", "+"))
        )
    return lines


def render_hepatotoxicity_report(resolution: EvidenceResolution) -> str:
    """Render `resolution` as the stable markdown artifact."""

    lines: list[str] = [
        REPORT_TITLE,
        "",
        "## Resolution Settings",
        "",
        f"- cascade version: {resolution.rules_version}",
        f"- evidence as of: {resolution.as_of}",
        "- networked tiers: "
        + ("enabled" if resolution.network_enabled else "disabled"),
        f"- DILIrank dataset: {resolution.dilirank_dataset}",
        f"- DILIrank workbook: {resolution.dilirank_path or 'unavailable'}",
        "",
        "## Item 6 Evidence By Drug",
        "",
    ]

    if resolution.findings:
        lines.extend(
            [
                "| Drug | Found via | Deciding source | Item 6 | Basis |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for finding in resolution.findings:
            lines.append(
                "| "
                + " | ".join(
                    (
                        _escape_cell(finding.drug_name),
                        _escape_cell(finding.origin),
                        _escape_cell(_TIER_LABELS.get(finding.tier, finding.tier)),
                        _escape_cell(_score_cell(finding.score)),
                        _escape_cell(finding.rationale),
                    )
                )
                + " |"
            )
    else:
        lines.append("No drug was resolved for this case.")

    lines.extend(["", "## Provenance", ""])
    if resolution.findings:
        for finding in resolution.findings:
            lines.extend(_render_finding(finding))
    else:
        lines.append("Not applicable.")

    lines.extend(["", "## Notes", ""])
    if resolution.notes:
        lines.extend(f"- {note}" for note in resolution.notes)
    else:
        lines.append("- None.")

    return "\n".join(lines).rstrip() + "\n"


def _render_finding(finding: HepatotoxicityEvidence) -> list[str]:
    lines = [f"### {finding.drug_name}", ""]
    if finding.matched_names:
        lines.append("- matched names: " + ", ".join(finding.matched_names))
    lines.append(f"- item 6: {_score_cell(finding.score)}")
    lines.append(
        "- deciding source: " + _TIER_LABELS.get(finding.tier, finding.tier)
    )
    if finding.dilirank:
        lines.extend(_render_dilirank(finding.dilirank))
    if finding.dailymed:
        lines.extend(_render_dailymed(finding.dailymed))
    if finding.literature:
        lines.extend(_render_literature(finding.literature))
    if finding.notes:
        lines.append("- notes:")
        lines.extend(f"  - {note}" for note in finding.notes)
    lines.append("")
    return lines


__all__ = ["REPORT_TITLE", "render_hepatotoxicity_report"]
