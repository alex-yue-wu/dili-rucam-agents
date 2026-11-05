"""Literature tier of the item 6 cascade: has this reaction been published?

The last tier asks PubMed for case reports pairing the drug with liver injury.
Two things keep the answer stable: the query is built from a fixed template, and
it carries an upper publication-date bound, so the count cannot creep upwards as
PubMed grows. One or more hits reaches the published rung; none scores zero.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import re
from typing import Mapping, Sequence

from .names import drug_name_keys
from .transport import JsonFetcher, build_url

PUBMED_ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
PUBMED_QUERY_URL = "https://pubmed.ncbi.nlm.nih.gov/?term={query}"
#: Sent so NCBI can attribute the traffic. No user identity is included.
PUBMED_TOOL_NAME = "dili-rucam-agents"
PUBMED_EARLIEST_DATE = "1900/01/01"

#: Liver-injury wording required alongside the drug name.
LIVER_INJURY_CLAUSE = (
    '("drug-induced liver injury"[Title/Abstract] '
    'OR hepatotoxicity[Title/Abstract] '
    'OR "liver injury"[Title/Abstract] '
    'OR "hepatic injury"[Title/Abstract] '
    'OR "liver failure"[Title/Abstract] '
    'OR "hepatic failure"[Title/Abstract] '
    'OR hepatitis[Title/Abstract] '
    'OR cholestasis[Title/Abstract] '
    'OR jaundice[Title/Abstract])'
)
#: RUCAM's published rung is about reported cases, not reviews.
CASE_REPORT_CLAUSE = (
    '("case reports"[Publication Type] OR "case report"[Title/Abstract])'
)

_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_QUOTE_RE = re.compile(r'["\\]')


@dataclass(frozen=True)
class LiteratureFinding:
    """Outcome of the literature tier for one drug name."""

    drug_name: str
    report_count: int
    pmids: tuple[str, ...]
    query: str
    as_of: str
    min_reports: int

    @property
    def score(self) -> int:
        return 1 if self.report_count >= self.min_reports else 0

    def to_dict(self) -> dict[str, object]:
        return {
            "drug_name": self.drug_name,
            "report_count": self.report_count,
            "score": self.score,
            "pmids": list(self.pmids),
            "query": self.query,
            "as_of": self.as_of,
            "min_reports": self.min_reports,
        }


def as_pubmed_date(as_of: str) -> str:
    """Convert an ISO `YYYY-MM-DD` date into PubMed's `YYYY/MM/DD` form."""

    match = _DATE_RE.match(str(as_of).strip())
    if not match:
        raise ValueError(f"as_of must be an ISO date (YYYY-MM-DD), got {as_of!r}")
    year, month, day = (int(part) for part in match.groups())
    date(year, month, day)  # rejects impossible dates such as 2026-02-31
    return f"{year:04d}/{month:02d}/{day:02d}"


def build_pubmed_query(drug_name: str, *, as_of: str, max_names: int = 4) -> str:
    """Build the fixed-template, date-bounded PubMed query for `drug_name`.

    A paper may use any of the drug's names, so the name clause ORs together the
    salt-stripped, spelling and synonym variants -- "frusemide" also searches
    "furosemide" -- rather than searching one spelling and reporting no reports.
    """

    cleaned = _QUOTE_RE.sub(" ", str(drug_name)).strip()
    if not cleaned:
        raise ValueError("drug_name must not be blank")
    upper_bound = as_pubmed_date(as_of)
    names: list[str] = [cleaned]
    for variant in drug_name_keys(cleaned)[: max(1, max_names)]:
        if variant.lower() not in {name.lower() for name in names}:
            names.append(variant)
    name_clause = " OR ".join(f'"{name}"[Title/Abstract]' for name in names)
    if len(names) > 1:
        name_clause = f"({name_clause})"
    return (
        f"{name_clause} "
        f"AND {LIVER_INJURY_CLAUSE} "
        f"AND {CASE_REPORT_CLAUSE} "
        f'AND ("{PUBMED_EARLIEST_DATE}"[PDAT] : "{upper_bound}"[PDAT])'
    )


def build_esearch_url(query: str, *, retmax: int = 5) -> str:
    """Deterministic esearch URL for `query`."""

    return build_url(
        PUBMED_ESEARCH_URL,
        {
            "db": "pubmed",
            "retmode": "json",
            "retmax": retmax,
            "sort": "pub_date",
            "term": query,
            "tool": PUBMED_TOOL_NAME,
        },
    )


def parse_esearch(payload: object) -> tuple[int, tuple[str, ...]]:
    """Read the hit count and sample PMIDs out of an esearch response."""

    if not isinstance(payload, Mapping):
        return 0, ()
    result = payload.get("esearchresult")
    if not isinstance(result, Mapping):
        return 0, ()
    try:
        count = int(result.get("count") or 0)
    except (TypeError, ValueError):
        count = 0
    raw_ids = result.get("idlist")
    pmids: tuple[str, ...] = ()
    if isinstance(raw_ids, Sequence) and not isinstance(raw_ids, (str, bytes)):
        pmids = tuple(str(value).strip() for value in raw_ids if str(value).strip())
    return max(count, 0), pmids


def resolve_literature(
    drug_name: str,
    *,
    as_of: str,
    fetch_json: JsonFetcher,
    min_reports: int = 1,
) -> LiteratureFinding:
    """Run the literature tier for `drug_name`."""

    query = build_pubmed_query(drug_name, as_of=as_of)
    count, pmids = parse_esearch(fetch_json(build_esearch_url(query)))
    return LiteratureFinding(
        drug_name=drug_name,
        report_count=count,
        pmids=pmids,
        query=query,
        as_of=as_of,
        min_reports=max(1, min_reports),
    )


def finding_from_cache(
    drug_name: str, entry: Mapping[str, object], *, as_of: str
) -> LiteratureFinding:
    """Rebuild a finding from a cached payload."""

    try:
        count = int(entry.get("report_count") or 0)
    except (TypeError, ValueError):
        count = 0
    try:
        min_reports = int(entry.get("min_reports") or 1)
    except (TypeError, ValueError):
        min_reports = 1
    raw_ids = entry.get("pmids")
    pmids: tuple[str, ...] = ()
    if isinstance(raw_ids, Sequence) and not isinstance(raw_ids, (str, bytes)):
        pmids = tuple(str(value) for value in raw_ids)
    return LiteratureFinding(
        drug_name=drug_name,
        report_count=max(count, 0),
        pmids=pmids,
        query=str(entry.get("query") or ""),
        as_of=str(entry.get("as_of") or as_of),
        min_reports=max(1, min_reports),
    )


__all__ = [
    "CASE_REPORT_CLAUSE",
    "LIVER_INJURY_CLAUSE",
    "LiteratureFinding",
    "PUBMED_EARLIEST_DATE",
    "PUBMED_ESEARCH_URL",
    "PUBMED_TOOL_NAME",
    "as_pubmed_date",
    "build_esearch_url",
    "build_pubmed_query",
    "finding_from_cache",
    "parse_esearch",
    "resolve_literature",
]
