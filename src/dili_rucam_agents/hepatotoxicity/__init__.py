"""Deterministic evidence sources for RUCAM item 6 (previous hepatotoxicity).

Item 6 is resolved before the analysts run, from DILIrank v2, then the DailyMed
label, then PubMed case reports. See `evidence.py` for the cascade itself.
"""

from .cache import EvidenceCache, cache_key
from .dailymed import DailyMedFinding, resolve_dailymed
from .dilirank import (
    DILIRANK_CONCERN_SCORES,
    DILIrankError,
    DILIrankRecord,
    DILIrankTable,
    default_dilirank_path,
    load_dilirank_table,
)
from .evidence import (
    BUNDLE_KEY,
    DEFAULT_EVIDENCE_AS_OF,
    EVIDENCE_RULES_VERSION,
    EvidenceResolution,
    EvidenceResolver,
    HepatotoxicityEvidence,
    annotate_case_bundle_payload,
    resolve_case_evidence,
)
from .literature import LiteratureFinding, build_pubmed_query, resolve_literature
from .names import drug_name_keys, normalize_drug_name
from .report import render_hepatotoxicity_report

__all__ = [
    "BUNDLE_KEY",
    "DEFAULT_EVIDENCE_AS_OF",
    "DILIRANK_CONCERN_SCORES",
    "DILIrankError",
    "DILIrankRecord",
    "DILIrankTable",
    "EVIDENCE_RULES_VERSION",
    "DailyMedFinding",
    "EvidenceCache",
    "EvidenceResolution",
    "EvidenceResolver",
    "HepatotoxicityEvidence",
    "LiteratureFinding",
    "annotate_case_bundle_payload",
    "build_pubmed_query",
    "cache_key",
    "default_dilirank_path",
    "drug_name_keys",
    "load_dilirank_table",
    "normalize_drug_name",
    "render_hepatotoxicity_report",
    "resolve_case_evidence",
    "resolve_dailymed",
    "resolve_literature",
]
