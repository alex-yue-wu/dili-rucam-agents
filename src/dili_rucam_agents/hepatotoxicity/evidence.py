"""The RUCAM item 6 evidence cascade.

Item 6 asks whether the drug's hepatotoxicity was already known. Left to the
analyst that question is answered from model memory, which is exactly where
scores diverged. This module answers it before any analyst runs, from three
sources consulted in a fixed order:

1. DILIrank v2, offline and pinned in the repository
2. the current FDA label on DailyMed
3. published case reports on PubMed

The first source that resolves the drug decides the score. Every answer carries
its provenance into the case bundle, so the analyst reads item 6 off a table
instead of recalling it, and a reviewer can retrace where the number came from.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import dailymed as dailymed_source
from . import literature as literature_source
from .cache import EvidenceCache, cache_key
from .dilirank import DILIrankError, DILIrankRecord, DILIrankTable, load_dilirank_table
from .names import normalize_drug_name
from .transport import FetchError, fetch_json as default_fetch_json
from .transport import fetch_text as default_fetch_text

#: Bump deliberately: moving the date changes every PubMed count.
DEFAULT_EVIDENCE_AS_OF = "2026-11-04"
EVIDENCE_RULES_VERSION = "RUCAM_item6_evidence_cascade_v1"
BUNDLE_KEY = "hepatotoxicity_evidence"

TIER_DILIRANK = "dilirank_v2"
TIER_DAILYMED = "dailymed_label"
TIER_LITERATURE = "pubmed_case_reports"
TIER_UNRESOLVED = "unresolved"

ORIGIN_BUNDLE_TEXT = "case_bundle_text"
ORIGIN_REQUESTED = "requested"

_DILIRANK_SECTION_LABELS = {
    "box warning": "Box warning",
    "warnings & precautions": "Warnings & precautions",
    "adverse reactions": "Adverse reactions",
    "no match": "No match",
    "withdrawn": "Withdrawn",
    "discontinued": "Discontinued",
}

_DAILYMED_SECTION_LABELS = {
    "boxed_warning": "Boxed warning",
    "warnings_and_precautions": "Warnings & precautions",
    "adverse_reactions": "Adverse reactions",
}


@dataclass(frozen=True)
class HepatotoxicityEvidence:
    """Resolved item 6 evidence for one drug name."""

    drug_name: str
    origin: str
    tier: str
    score: int | None
    rationale: str
    matched_names: tuple[str, ...] = ()
    dilirank: tuple[dict[str, Any], ...] = ()
    dailymed: dict[str, Any] | None = None
    literature: dict[str, Any] | None = None
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "drug_name": self.drug_name,
            "origin": self.origin,
            "tier": self.tier,
            "score": self.score,
            "rationale": self.rationale,
            "matched_names": list(self.matched_names),
            "notes": list(self.notes),
        }
        if self.dilirank:
            payload["dilirank"] = [dict(row) for row in self.dilirank]
        if self.dailymed is not None:
            payload["dailymed"] = dict(self.dailymed)
        if self.literature is not None:
            payload["literature"] = dict(self.literature)
        return payload


@dataclass(frozen=True)
class EvidenceResolution:
    """Every resolved drug for one case, plus how the cascade was configured."""

    as_of: str
    network_enabled: bool
    dilirank_dataset: str
    dilirank_path: str
    findings: tuple[HepatotoxicityEvidence, ...] = ()
    notes: tuple[str, ...] = ()
    rules_version: str = EVIDENCE_RULES_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "rules_version": self.rules_version,
            "as_of": self.as_of,
            "network_enabled": self.network_enabled,
            "dilirank_dataset": self.dilirank_dataset,
            "dilirank_path": self.dilirank_path,
            "notes": list(self.notes),
            "drugs": [finding.to_dict() for finding in self.findings],
        }

    def for_drug(self, drug_name: str) -> HepatotoxicityEvidence | None:
        wanted = normalize_drug_name(drug_name)
        for finding in self.findings:
            if normalize_drug_name(finding.drug_name) == wanted:
                return finding
        return None


@dataclass
class EvidenceResolver:
    """Runs the cascade, consulting the cache before the network."""

    table: DILIrankTable
    as_of: str = DEFAULT_EVIDENCE_AS_OF
    cache: EvidenceCache = field(default_factory=EvidenceCache)
    network_enabled: bool = True
    max_labels: int = 3
    min_reports: int = 1
    fetch_json: Callable[[str], Any] = default_fetch_json
    fetch_text: Callable[[str], str] = default_fetch_text

    def resolve(self, drug_name: str, *, origin: str = ORIGIN_REQUESTED) -> HepatotoxicityEvidence:
        cleaned = str(drug_name).strip()
        if not cleaned:
            raise ValueError("drug_name must not be blank")

        records = self.table.lookup(cleaned)
        if records:
            return self._from_dilirank(cleaned, origin, records)

        if not self.network_enabled:
            return HepatotoxicityEvidence(
                drug_name=cleaned,
                origin=origin,
                tier=TIER_UNRESOLVED,
                score=None,
                rationale=(
                    "Not listed in DILIrank v2 and the networked tiers are disabled."
                ),
                notes=("DailyMed and PubMed were not consulted.",),
            )

        notes: list[str] = []
        dailymed_payload: dict[str, Any] | None = None
        try:
            finding = self._dailymed(cleaned)
        except FetchError as error:
            notes.append(f"DailyMed lookup failed: {error}")
        else:
            dailymed_payload = finding.to_dict()
            if finding.section is not None:
                return HepatotoxicityEvidence(
                    drug_name=cleaned,
                    origin=origin,
                    tier=TIER_DAILYMED,
                    score=finding.score,
                    rationale=(
                        "Liver injury appears in the "
                        f"{_DAILYMED_SECTION_LABELS[finding.section]} section of the "
                        "current DailyMed label."
                    ),
                    dailymed=dailymed_payload,
                    notes=tuple(notes),
                )
            if finding.label_found:
                notes.append(
                    "A DailyMed label exists but none of its boxed warning, "
                    "warnings and precautions or adverse reactions sections "
                    "mentions liver injury."
                )
            else:
                notes.append("No current DailyMed label matched this drug name.")

        try:
            published = self._literature(cleaned)
        except FetchError as error:
            notes.append(f"PubMed lookup failed: {error}")
            return HepatotoxicityEvidence(
                drug_name=cleaned,
                origin=origin,
                tier=TIER_UNRESOLVED,
                score=None,
                rationale="No source resolved this drug.",
                dailymed=dailymed_payload,
                notes=tuple(notes),
            )

        rationale = (
            f"{published.report_count} PubMed case report(s) pair this drug with "
            f"liver injury as of {published.as_of}."
            if published.report_count
            else (
                "No PubMed case report pairs this drug with liver injury as of "
                f"{published.as_of}."
            )
        )
        return HepatotoxicityEvidence(
            drug_name=cleaned,
            origin=origin,
            tier=TIER_LITERATURE,
            score=published.score,
            rationale=rationale,
            dailymed=dailymed_payload,
            literature=published.to_dict(),
            notes=tuple(notes),
        )

    def _from_dilirank(
        self, drug_name: str, origin: str, records: Sequence[DILIrankRecord]
    ) -> HepatotoxicityEvidence:
        scored = [record for record in records if record.score is not None]
        notes: list[str] = []
        if not scored:
            return HepatotoxicityEvidence(
                drug_name=drug_name,
                origin=origin,
                tier=TIER_UNRESOLVED,
                score=None,
                rationale=(
                    "Listed in DILIrank v2 with an unrecognised concern class: "
                    + ", ".join(sorted({record.concern for record in records}))
                ),
                matched_names=tuple(record.compound_name for record in records),
                dilirank=tuple(record.to_dict() for record in records),
            )

        distinct = sorted({record.score for record in scored})
        score = distinct[0]
        if len(distinct) > 1:
            notes.append(
                "This name matches several DILIrank rows scoring "
                + ", ".join(str(value) for value in distinct)
                + "; the lowest was taken (convention D4)."
            )
        chosen = [record for record in scored if record.score == score]
        concerns = sorted({record.concern for record in chosen})
        sections = sorted(
            {
                _DILIRANK_SECTION_LABELS.get(
                    record.label_section.lower(), record.label_section
                )
                for record in chosen
                if record.label_section
            }
        )
        rationale = "DILIrank v2 class " + "/".join(concerns)
        if sections:
            rationale += " (label section: " + "/".join(sections) + ")"
        return HepatotoxicityEvidence(
            drug_name=drug_name,
            origin=origin,
            tier=TIER_DILIRANK,
            score=score,
            rationale=rationale + ".",
            matched_names=tuple(record.compound_name for record in scored),
            dilirank=tuple(record.to_dict() for record in scored),
            notes=tuple(notes),
        )

    def _dailymed(self, drug_name: str) -> dailymed_source.DailyMedFinding:
        key = cache_key("dailymed", drug_name, self.as_of)
        cached = self.cache.get(key)
        if cached is not None:
            return dailymed_source.finding_from_cache(drug_name, cached)
        finding = dailymed_source.resolve_dailymed(
            drug_name,
            fetch_json=self.fetch_json,
            fetch_text=self.fetch_text,
            max_labels=self.max_labels,
        )
        self.cache.put(key, finding.to_dict())
        return finding

    def _literature(self, drug_name: str) -> literature_source.LiteratureFinding:
        key = cache_key("pubmed", drug_name, self.as_of)
        cached = self.cache.get(key)
        if cached is not None:
            return literature_source.finding_from_cache(
                drug_name, cached, as_of=self.as_of
            )
        finding = literature_source.resolve_literature(
            drug_name,
            as_of=self.as_of,
            fetch_json=self.fetch_json,
            min_reports=self.min_reports,
        )
        self.cache.put(key, finding.to_dict())
        return finding


def collect_scan_text(payload: Mapping[str, Any]) -> str:
    """Concatenate every text surface of a case bundle for the name scan."""

    parts: list[str] = []
    normalized = payload.get("normalized_text")
    if isinstance(normalized, str):
        parts.append(normalized)
    blocks = payload.get("blocks")
    if isinstance(blocks, Iterable) and not isinstance(blocks, (str, bytes)):
        for block in blocks:
            if isinstance(block, Mapping) and isinstance(block.get("text"), str):
                parts.append(block["text"])
    tables = payload.get("tables")
    if isinstance(tables, Iterable) and not isinstance(tables, (str, bytes)):
        for table in tables:
            if not isinstance(table, Mapping):
                continue
            if isinstance(table.get("preview"), str):
                parts.append(table["preview"])
            rows = table.get("raw_rows")
            if isinstance(rows, Iterable) and not isinstance(rows, (str, bytes)):
                for row in rows:
                    if isinstance(row, Iterable) and not isinstance(row, (str, bytes)):
                        parts.append(
                            " ".join(str(cell) for cell in row if cell is not None)
                        )
    return "\n".join(parts)


def resolve_case_evidence(
    payload: Mapping[str, Any],
    *,
    drug_names: Sequence[str] = (),
    table: DILIrankTable | None = None,
    dilirank_path: str | None = None,
    as_of: str = DEFAULT_EVIDENCE_AS_OF,
    cache: EvidenceCache | None = None,
    network_enabled: bool = True,
    max_labels: int = 3,
    min_reports: int = 1,
    fetch_json: Callable[[str], Any] = default_fetch_json,
    fetch_text: Callable[[str], str] = default_fetch_text,
) -> EvidenceResolution:
    """Resolve item 6 evidence for every drug this case names.

    Names come from two places: a scan of the bundle text against the DILIrank
    index, and `drug_names` supplied by the caller for drugs DILIrank does not
    list. Only supplied names can reach the DailyMed and PubMed tiers, because
    nothing else in the deterministic layer knows which words are drugs.
    """

    resolution_notes: list[str] = []
    try:
        resolved_table = (
            table if table is not None else load_dilirank_table(dilirank_path)
        )
    except DILIrankError as error:
        return EvidenceResolution(
            as_of=as_of,
            network_enabled=network_enabled,
            dilirank_dataset="unavailable",
            dilirank_path="",
            notes=(f"DILIrank lookup unavailable: {error}",),
        )

    resolver = EvidenceResolver(
        table=resolved_table,
        as_of=as_of,
        cache=cache if cache is not None else EvidenceCache(),
        network_enabled=network_enabled,
        max_labels=max_labels,
        min_reports=min_reports,
        fetch_json=fetch_json,
        fetch_text=fetch_text,
    )

    requested = {
        normalize_drug_name(name): str(name).strip()
        for name in drug_names
        if str(name).strip()
    }
    # The matched key is used verbatim rather than the first row's canonical
    # name: a key such as "metoprolol" covers several DILIrank rows, and
    # canonicalising it to one of them would hide the disagreement that
    # convention D4 exists to resolve.
    discovered = tuple(
        key
        for key, _records in resolved_table.find_in_text(collect_scan_text(payload))
        if key not in requested
    )

    findings: list[HepatotoxicityEvidence] = []
    for key in sorted(requested):
        findings.append(resolver.resolve(requested[key], origin=ORIGIN_REQUESTED))
    for key in sorted(discovered):
        findings.append(resolver.resolve(key, origin=ORIGIN_BUNDLE_TEXT))

    if not findings:
        resolution_notes.append(
            "No DILIrank compound name was found in the case bundle text and no "
            "drug name was supplied, so item 6 has no pre-resolved evidence."
        )
    if not network_enabled:
        resolution_notes.append(
            "Networked tiers disabled: DailyMed and PubMed were not consulted."
        )

    return EvidenceResolution(
        as_of=as_of,
        network_enabled=network_enabled,
        dilirank_dataset=resolved_table.dataset_label,
        dilirank_path=resolved_table.source_path,
        findings=tuple(findings),
        notes=tuple(resolution_notes),
    )


def annotate_case_bundle_payload(
    payload: Mapping[str, Any], **kwargs: Any
) -> dict[str, Any]:
    """Return `payload` with the resolved item 6 evidence block attached."""

    annotated = dict(payload)
    annotated[BUNDLE_KEY] = resolve_case_evidence(payload, **kwargs).to_dict()
    return annotated


__all__ = [
    "BUNDLE_KEY",
    "DEFAULT_EVIDENCE_AS_OF",
    "EVIDENCE_RULES_VERSION",
    "EvidenceResolution",
    "EvidenceResolver",
    "HepatotoxicityEvidence",
    "ORIGIN_BUNDLE_TEXT",
    "ORIGIN_REQUESTED",
    "TIER_DAILYMED",
    "TIER_DILIRANK",
    "TIER_LITERATURE",
    "TIER_UNRESOLVED",
    "annotate_case_bundle_payload",
    "collect_scan_text",
    "resolve_case_evidence",
]
