"""On-disk cache for the networked tiers of the item 6 evidence cascade.

A RUCAM score must not depend on the day the pipeline ran. DailyMed labels are
revised and PubMed counts grow, so every networked answer is written to a JSON
cache keyed by source, as-of date and drug name. Later runs read the cache and
the network is touched only for a key that has never been resolved. The cache
file is therefore the reproducibility record and is meant to be committed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Callable

from .names import normalize_drug_name

CACHE_FORMAT = "dili-rucam-hepatotoxicity-cache/1"


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def cache_key(source: str, drug_name: str, as_of: str) -> str:
    """Stable key for one resolved lookup."""

    return f"{source}|{as_of}|{normalize_drug_name(drug_name)}"


@dataclass
class EvidenceCache:
    """Mapping of cache key to resolved payload, optionally file-backed."""

    path: Path | None = None
    entries: dict[str, dict[str, Any]] = field(default_factory=dict)
    clock: Callable[[], str] = _utc_now
    _dirty: bool = False

    @classmethod
    def load(
        cls, path: Path | str | None, *, clock: Callable[[], str] = _utc_now
    ) -> "EvidenceCache":
        """Read the cache at `path`, treating a missing or unreadable file as empty."""

        if path is None:
            return cls(path=None, clock=clock)
        resolved = Path(path).expanduser()
        entries: dict[str, dict[str, Any]] = {}
        if resolved.exists():
            try:
                raw = json.loads(resolved.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raw = {}
            stored = raw.get("entries") if isinstance(raw, dict) else None
            if isinstance(stored, dict):
                entries = {
                    key: value
                    for key, value in stored.items()
                    if isinstance(key, str) and isinstance(value, dict)
                }
        return cls(path=resolved, entries=entries, clock=clock)

    def get(self, key: str) -> dict[str, Any] | None:
        entry = self.entries.get(key)
        return dict(entry) if entry is not None else None

    def put(self, key: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Store `payload` under `key`, stamping when it was fetched."""

        entry = dict(payload)
        entry.setdefault("fetched_at", self.clock())
        self.entries[key] = entry
        self._dirty = True
        return dict(entry)

    @property
    def dirty(self) -> bool:
        return self._dirty

    def save(self) -> bool:
        """Write the cache back if it is file-backed and has new entries."""

        if self.path is None or not self._dirty:
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        document = {
            "format": CACHE_FORMAT,
            "entries": {key: self.entries[key] for key in sorted(self.entries)},
        }
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        temporary.replace(self.path)
        self._dirty = False
        return True


__all__ = ["CACHE_FORMAT", "EvidenceCache", "cache_key"]
