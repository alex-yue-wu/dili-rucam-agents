"""DailyMed tier of the item 6 cascade: is the reaction in the FDA label?

A drug absent from DILIrank may still carry a current label. This tier asks
DailyMed for that label's structured sections and reports the strongest section
that mentions liver injury: a boxed warning or warnings-and-precautions
statement reaches the labelled rung, an adverse-reactions listing reaches the
published rung.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable, Mapping, Sequence
import xml.etree.ElementTree as ElementTree

from .names import drug_name_keys
from .transport import JsonFetcher, TextFetcher, build_url

DAILYMED_SEARCH_URL = "https://dailymed.nlm.nih.gov/dailymed/services/v2/spls.json"
DAILYMED_SPL_URL = "https://dailymed.nlm.nih.gov/dailymed/services/v2/spls/{setid}.xml"
DAILYMED_LABEL_URL = "https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid={setid}"

_SPL_NAMESPACE = "{urn:hl7-org:v3}"

#: LOINC section codes grouped into the label sections RUCAM item 6 cares about.
BOXED_WARNING_CODES = frozenset({"34066-1"})
WARNINGS_AND_PRECAUTIONS_CODES = frozenset(
    {
        "43685-7",  # WARNINGS AND PRECAUTIONS (PLR format)
        "34071-1",  # WARNINGS (legacy format)
        "42232-9",  # PRECAUTIONS (legacy format)
        "34072-9",  # GENERAL PRECAUTIONS (legacy format)
    }
)
ADVERSE_REACTIONS_CODES = frozenset({"34084-4"})

SECTION_CODE_GROUPS: Mapping[str, frozenset[str]] = {
    "boxed_warning": BOXED_WARNING_CODES,
    "warnings_and_precautions": WARNINGS_AND_PRECAUTIONS_CODES,
    "adverse_reactions": ADVERSE_REACTIONS_CODES,
}

#: Strongest section first; the first hit decides the score.
SECTION_PRECEDENCE: tuple[str, ...] = (
    "boxed_warning",
    "warnings_and_precautions",
    "adverse_reactions",
)

DAILYMED_SECTION_SCORES: Mapping[str, int] = {
    "boxed_warning": 2,
    "warnings_and_precautions": 2,
    "adverse_reactions": 1,
}

#: Wording that reports liver injury.
_HEPATIC_INJURY_TERMS = (
    r"hepatotoxic\w*",
    r"hepatitis",
    r"hepatic\s+(?:failure|necrosis|injur\w*|events?|reactions?|disorders?|toxicit\w*|decompensation)",
    r"liver\s+(?:failure|necrosis|injur\w*|damage|toxicit\w*|disease|transplant\w*|enzymes?|function\s+tests?)",
    r"hepatocellular\s+(?:injur\w*|damage|necrosis)",
    r"drug[-\s]induced\s+liver\s+injury",
    r"jaundice",
    r"icterus",
    r"cholestas\w*",
    r"cholestatic",
    r"transaminas\w*",
    r"aminotransferas\w*",
    r"bilirubin",
    r"hepatomegal\w*",
    r"steatohepatitis",
    r"hepatic\s+steatosis",
    r"veno-?occlusive",
    r"hy'?s\s+law",
    r"\bALT\b",
    r"\bAST\b",
    r"\bSGOT\b",
    r"\bSGPT\b",
    r"\bLFTs?\b",
    r"\bDILI\b",
)

#: Pre-existing liver disease as a dosing caveat is not a reported reaction.
_NON_INJURY_PHRASES = (
    r"hepatic\s+impairment",
    r"hepatic\s+insufficiency",
    r"hepatically\s+impaired",
    r"liver\s+impairment",
)

_HEPATIC_INJURY_RE = re.compile("|".join(_HEPATIC_INJURY_TERMS), re.IGNORECASE)
_NON_INJURY_RE = re.compile("|".join(_NON_INJURY_PHRASES), re.IGNORECASE)
_WHITESPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class SplReference:
    """One Structured Product Label returned by the DailyMed search."""

    setid: str
    title: str
    spl_version: str
    published_date: str

    def to_dict(self) -> dict[str, object]:
        return {
            "setid": self.setid,
            "title": self.title,
            "spl_version": self.spl_version,
            "published_date": self.published_date,
            "url": DAILYMED_LABEL_URL.format(setid=self.setid),
        }


@dataclass(frozen=True)
class SplClassification:
    """Which label sections of one SPL report liver injury."""

    sections: tuple[str, ...]
    matched_terms: tuple[str, ...]

    @property
    def strongest_section(self) -> str | None:
        for section in SECTION_PRECEDENCE:
            if section in self.sections:
                return section
        return None


@dataclass(frozen=True)
class DailyMedFinding:
    """Outcome of the DailyMed tier for one drug name."""

    drug_name: str
    label_found: bool
    section: str | None
    matched_terms: tuple[str, ...]
    labels: tuple[SplReference, ...]
    search_url: str
    total_labels: int
    search_name: str | None = None
    searched_names: tuple[str, ...] = ()

    @property
    def score(self) -> int | None:
        if self.section is None:
            return None
        return DAILYMED_SECTION_SCORES[self.section]

    def to_dict(self) -> dict[str, object]:
        return {
            "drug_name": self.drug_name,
            "label_found": self.label_found,
            "section": self.section,
            "score": self.score,
            "matched_terms": list(self.matched_terms),
            "labels": [label.to_dict() for label in self.labels],
            "search_url": self.search_url,
            "total_labels": self.total_labels,
            "search_name": self.search_name,
            "searched_names": list(self.searched_names),
        }


def build_search_url(drug_name: str, *, page_size: int = 25) -> str:
    """Deterministic DailyMed search URL for `drug_name`."""

    return build_url(
        DAILYMED_SEARCH_URL, {"drug_name": drug_name, "pagesize": page_size}
    )


def parse_spl_search(payload: object) -> tuple[tuple[SplReference, ...], int]:
    """Read the search response into setid-ordered references plus the total count."""

    if not isinstance(payload, Mapping):
        return (), 0
    rows = payload.get("data")
    metadata = payload.get("metadata")
    total = 0
    if isinstance(metadata, Mapping):
        try:
            total = int(metadata.get("total_elements") or 0)
        except (TypeError, ValueError):
            total = 0
    references: list[SplReference] = []
    if isinstance(rows, Iterable):
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            setid = str(row.get("setid") or "").strip()
            if not setid:
                continue
            references.append(
                SplReference(
                    setid=setid,
                    title=str(row.get("title") or "").strip(),
                    spl_version=str(row.get("spl_version") or "").strip(),
                    published_date=str(row.get("published_date") or "").strip(),
                )
            )
    # setid is immutable, unlike publication order, so sorting on it keeps the
    # chosen labels stable between runs.
    ordered = tuple(sorted(references, key=lambda reference: reference.setid))
    return ordered, max(total, len(ordered))


def _section_group(code: str) -> str | None:
    for group, codes in SECTION_CODE_GROUPS.items():
        if code in codes:
            return group
    return None


def find_hepatic_terms(text: str) -> tuple[str, ...]:
    """Distinct liver-injury terms in `text`, ignoring dosing-caveat wording."""

    if not text:
        return ()
    collapsed = _WHITESPACE_RE.sub(" ", text)
    scrubbed = _NON_INJURY_RE.sub(" ", collapsed)
    seen: dict[str, None] = {}
    for match in _HEPATIC_INJURY_RE.finditer(scrubbed):
        seen.setdefault(_WHITESPACE_RE.sub(" ", match.group(0)).strip().lower(), None)
    return tuple(sorted(seen))


def classify_spl_xml(xml_text: str) -> SplClassification:
    """Report which of the three label sections of one SPL mention liver injury."""

    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return SplClassification(sections=(), matched_terms=())

    sections: dict[str, None] = {}
    terms: dict[str, None] = {}
    for section in root.iter(f"{_SPL_NAMESPACE}section"):
        code_element = section.find(f"{_SPL_NAMESPACE}code")
        if code_element is None:
            continue
        group = _section_group(str(code_element.get("code") or "").strip())
        if group is None:
            continue
        matched = find_hepatic_terms(" ".join(section.itertext()))
        if not matched:
            continue
        sections.setdefault(group, None)
        for term in matched:
            terms.setdefault(term, None)

    return SplClassification(
        sections=tuple(
            section for section in SECTION_PRECEDENCE if section in sections
        ),
        matched_terms=tuple(sorted(terms)),
    )


def resolve_dailymed(
    drug_name: str,
    *,
    fetch_json: JsonFetcher,
    fetch_text: TextFetcher,
    max_labels: int = 3,
    max_queries: int = 4,
) -> DailyMedFinding:
    """Run the DailyMed tier for `drug_name`.

    DailyMed matches substrings of the names a label itself carries, so it finds
    "Amiodarone hydrochloride" from "amiodarone" but nothing at all from
    "morphine sulphate" or "ciclosporin". The drug name is therefore expanded
    into its salt-stripped, spelling and synonym variants, and at most
    `max_queries` of them are searched in order until one returns a label.

    At most `max_labels` SPLs are then read, chosen by ascending setid, and the
    strongest section found across them decides the score.
    """

    candidates = drug_name_keys(drug_name)[: max(1, max_queries)] or (drug_name,)
    searched: list[str] = []
    references: tuple[SplReference, ...] = ()
    total = 0
    search_url = build_search_url(candidates[0])
    for candidate in candidates:
        searched.append(candidate)
        search_url = build_search_url(candidate)
        references, total = parse_spl_search(fetch_json(search_url))
        if references:
            break

    if not references:
        return DailyMedFinding(
            drug_name=drug_name,
            label_found=False,
            section=None,
            matched_terms=(),
            labels=(),
            search_url=search_url,
            total_labels=total,
            search_name=None,
            searched_names=tuple(searched),
        )

    inspected = references[: max(1, max_labels)]
    found_sections: dict[str, None] = {}
    terms: dict[str, None] = {}
    for reference in inspected:
        classification = classify_spl_xml(
            fetch_text(DAILYMED_SPL_URL.format(setid=reference.setid))
        )
        for section in classification.sections:
            found_sections.setdefault(section, None)
        for term in classification.matched_terms:
            terms.setdefault(term, None)

    strongest = next(
        (section for section in SECTION_PRECEDENCE if section in found_sections), None
    )
    return DailyMedFinding(
        drug_name=drug_name,
        label_found=True,
        section=strongest,
        matched_terms=tuple(sorted(terms)),
        labels=inspected,
        search_url=search_url,
        total_labels=total,
        search_name=searched[-1],
        searched_names=tuple(searched),
    )


def finding_from_cache(drug_name: str, entry: Mapping[str, object]) -> DailyMedFinding:
    """Rebuild a finding from a cached payload."""

    labels_raw = entry.get("labels")
    labels: list[SplReference] = []
    if isinstance(labels_raw, Sequence) and not isinstance(labels_raw, (str, bytes)):
        for row in labels_raw:
            if not isinstance(row, Mapping):
                continue
            labels.append(
                SplReference(
                    setid=str(row.get("setid") or ""),
                    title=str(row.get("title") or ""),
                    spl_version=str(row.get("spl_version") or ""),
                    published_date=str(row.get("published_date") or ""),
                )
            )
    section = entry.get("section")
    terms_raw = entry.get("matched_terms")
    try:
        total = int(entry.get("total_labels") or 0)
    except (TypeError, ValueError):
        total = 0
    searched_raw = entry.get("searched_names")
    search_name = entry.get("search_name")
    return DailyMedFinding(
        drug_name=drug_name,
        label_found=bool(entry.get("label_found")),
        section=str(section) if section else None,
        matched_terms=tuple(
            str(term)
            for term in (
                terms_raw
                if isinstance(terms_raw, Sequence) and not isinstance(terms_raw, str)
                else ()
            )
        ),
        labels=tuple(labels),
        search_url=str(entry.get("search_url") or ""),
        total_labels=total,
        search_name=str(search_name) if search_name else None,
        searched_names=tuple(
            str(name)
            for name in (
                searched_raw
                if isinstance(searched_raw, Sequence)
                and not isinstance(searched_raw, str)
                else ()
            )
        ),
    )


__all__ = [
    "ADVERSE_REACTIONS_CODES",
    "BOXED_WARNING_CODES",
    "DAILYMED_SEARCH_URL",
    "DAILYMED_SECTION_SCORES",
    "DAILYMED_SPL_URL",
    "DailyMedFinding",
    "SECTION_PRECEDENCE",
    "SplClassification",
    "SplReference",
    "WARNINGS_AND_PRECAUTIONS_CODES",
    "build_search_url",
    "classify_spl_xml",
    "finding_from_cache",
    "find_hepatic_terms",
    "parse_spl_search",
    "resolve_dailymed",
]
