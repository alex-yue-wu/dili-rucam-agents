"""Configuration for the item 6 evidence cascade, as one hashable value.

The resolved evidence changes what the analysts read, so the settings that
produced it belong in the analyst checkpoint fingerprint. Keeping them in a
single frozen dataclass means the pipeline passes one argument and the
fingerprint covers every knob.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from typing import Any, Callable, Mapping

from .cache import EvidenceCache
from .evidence import (
    BUNDLE_KEY,
    DEFAULT_EVIDENCE_AS_OF,
    EvidenceResolution,
    resolve_case_evidence,
)
from .transport import fetch_json as default_fetch_json
from .transport import fetch_text as default_fetch_text

#: Default cache location, relative to the repository root.
DEFAULT_CACHE_RELATIVE_PATH = "dilirank_v2/hepatotoxicity-evidence-cache.json"


@dataclass(frozen=True)
class HepatotoxicityEvidenceSettings:
    """How to resolve item 6 evidence for one run."""

    enabled: bool = True
    drug_names: tuple[str, ...] = ()
    network_enabled: bool = True
    as_of: str = DEFAULT_EVIDENCE_AS_OF
    cache_path: str | None = None
    dilirank_path: str | None = None
    max_labels: int = 3
    min_reports: int = 1

    def fingerprint(self) -> str:
        """Stable hash of every setting that can change the resolved evidence."""

        payload = asdict(self)
        payload["drug_names"] = sorted(self.drug_names)
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


def apply_hepatotoxicity_evidence(
    payload: Mapping[str, Any],
    settings: HepatotoxicityEvidenceSettings,
    *,
    fetch_json: Callable[[str], Any] = default_fetch_json,
    fetch_text: Callable[[str], str] = default_fetch_text,
) -> tuple[dict[str, Any], EvidenceResolution | None]:
    """Resolve the cascade for `payload` and attach the evidence block.

    Returns the annotated copy of the payload and the resolution, or the payload
    unchanged and None when the cascade is disabled.
    """

    annotated = dict(payload)
    if not settings.enabled:
        return annotated, None

    cache = EvidenceCache.load(settings.cache_path)
    resolution = resolve_case_evidence(
        payload,
        drug_names=settings.drug_names,
        as_of=settings.as_of,
        cache=cache,
        network_enabled=settings.network_enabled,
        max_labels=settings.max_labels,
        min_reports=settings.min_reports,
        fetch_json=fetch_json,
        fetch_text=fetch_text,
        dilirank_path=settings.dilirank_path,
    )
    cache.save()
    annotated[BUNDLE_KEY] = resolution.to_dict()
    return annotated, resolution


__all__ = [
    "DEFAULT_CACHE_RELATIVE_PATH",
    "HepatotoxicityEvidenceSettings",
    "apply_hepatotoxicity_evidence",
]
