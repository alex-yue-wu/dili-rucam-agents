"""Offline lookup against the FDA DILIrank dataset (version 2).

DILIrank assigns every listed compound a `vDILI-Concern` class derived from the
hepatotoxicity wording of its FDA label, plus the label section that wording
came from. That makes it the one tier of the item 6 cascade that needs no
network and cannot drift between analysts: the workbook is pinned in the
repository and the mapping from concern class to RUCAM points is a constant.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import re
from typing import Mapping, Sequence

from openpyxl import load_workbook

from .names import drug_name_keys, normalize_drug_name

#: Points awarded for each DILIrank concern class, keyed by lower-cased class.
#: Most- and Less-DILI-concern both mean the FDA label carries hepatotoxicity
#: wording, so both reach the labelled rung; Ambiguous means the evidence is
#: contested, which is the published-reports rung; No-DILI-concern means the
#: label has no hepatotoxicity section at all.
DILIRANK_CONCERN_SCORES: Mapping[str, int] = {
    "vmost-dili-concern": 2,
    "vless-dili-concern": 2,
    "ambiguous-dili-concern": 1,
    "vno-dili-concern": 0,
}

_SHEET_NAME = "version 2"
_REQUIRED_COLUMNS = (
    "LTKBID",
    "CompoundName",
    "SeverityClass",
    "LabelSection",
    "vDILI-Concern",
)
_MIN_TEXT_SCAN_KEY_LENGTH = 4


class DILIrankError(RuntimeError):
    """Raised when the pinned DILIrank workbook cannot be read as expected."""


@dataclass(frozen=True)
class DILIrankRecord:
    """One compound row of the DILIrank workbook."""

    ltkb_id: str
    compound_name: str
    severity_class: int | None
    label_section: str
    concern: str

    @property
    def concern_key(self) -> str:
        return self.concern.strip().lower()

    @property
    def score(self) -> int | None:
        """RUCAM item 6 points for this row, or None for an unknown class."""

        return DILIRANK_CONCERN_SCORES.get(self.concern_key)

    def to_dict(self) -> dict[str, object]:
        return {
            "ltkb_id": self.ltkb_id,
            "compound_name": self.compound_name,
            "severity_class": self.severity_class,
            "label_section": self.label_section,
            "vdili_concern": self.concern,
            "score": self.score,
        }


@dataclass(frozen=True)
class DILIrankTable:
    """Indexed, read-only view of the workbook."""

    dataset_label: str
    source_path: str
    records: tuple[DILIrankRecord, ...]
    index: Mapping[str, tuple[DILIrankRecord, ...]]
    _text_scan_re: re.Pattern[str] | None

    def lookup(self, name: str) -> tuple[DILIrankRecord, ...]:
        """Records matching `name`, trying the exact key before the salt-stripped one."""

        for key in drug_name_keys(name):
            matches = self.index.get(key)
            if matches:
                return matches
        return ()

    def find_in_text(self, text: str) -> tuple[tuple[str, tuple[DILIrankRecord, ...]], ...]:
        """Every indexed compound name occurring in `text`, in alphabetical order."""

        if not text or self._text_scan_re is None:
            return ()
        hits = {
            normalize_drug_name(match.group(0))
            for match in self._text_scan_re.finditer(text)
        }
        resolved: list[tuple[str, tuple[DILIrankRecord, ...]]] = []
        for key in sorted(hits):
            matches = self.index.get(key)
            if matches:
                resolved.append((key, matches))
        return tuple(resolved)


def default_dilirank_path() -> Path:
    """Path of the workbook pinned at the repository root."""

    return Path(__file__).resolve().parents[3] / "dilirank_v2" / "DILIrank_v2.xlsx"


def _header_positions(header_row: Sequence[object]) -> dict[str, int]:
    positions = {
        str(value).strip(): index
        for index, value in enumerate(header_row)
        if value is not None and str(value).strip()
    }
    missing = [column for column in _REQUIRED_COLUMNS if column not in positions]
    if missing:
        raise DILIrankError(
            "DILIrank workbook is missing expected columns: " + ", ".join(missing)
        )
    return positions


def _coerce_severity(value: object) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _build_text_scan_re(keys: Sequence[str]) -> re.Pattern[str] | None:
    scannable = sorted(
        (key for key in keys if len(key) >= _MIN_TEXT_SCAN_KEY_LENGTH),
        key=lambda key: (-len(key), key),
    )
    if not scannable:
        return None
    # PDF extraction breaks multi-word names across lines, so a literal space
    # in a key has to match any run of whitespace.
    alternatives = [
        r"\s+".join(re.escape(token) for token in key.split())
        for key in scannable
    ]
    return re.compile(
        r"(?<![0-9A-Za-z])(?:" + "|".join(alternatives) + r")(?![0-9A-Za-z])",
        re.IGNORECASE,
    )


def build_dilirank_table(path: Path) -> DILIrankTable:
    """Read and index the workbook at `path` with no caching."""

    if not path.exists():
        raise DILIrankError(f"DILIrank workbook not found: {path}")

    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        if _SHEET_NAME not in workbook.sheetnames:
            raise DILIrankError(
                f"DILIrank workbook {path} has no {_SHEET_NAME!r} sheet; "
                f"found {workbook.sheetnames}"
            )
        rows = list(workbook[_SHEET_NAME].iter_rows(values_only=True))
    finally:
        workbook.close()

    if len(rows) < 3:
        raise DILIrankError(f"DILIrank workbook {path} holds no data rows")

    dataset_label = str(rows[0][0] or "").strip() or "DILIrank (version 2)"
    positions = _header_positions(rows[1])

    records: list[DILIrankRecord] = []
    for row in rows[2:]:
        compound_name = row[positions["CompoundName"]]
        if compound_name is None or not str(compound_name).strip():
            continue
        records.append(
            DILIrankRecord(
                ltkb_id=str(row[positions["LTKBID"]] or "").strip(),
                compound_name=str(compound_name).strip(),
                severity_class=_coerce_severity(row[positions["SeverityClass"]]),
                label_section=str(row[positions["LabelSection"]] or "").strip(),
                concern=str(row[positions["vDILI-Concern"]] or "").strip(),
            )
        )

    if not records:
        raise DILIrankError(f"DILIrank workbook {path} holds no compound rows")

    grouped: dict[str, list[DILIrankRecord]] = {}
    for record in records:
        for key in drug_name_keys(record.compound_name):
            grouped.setdefault(key, []).append(record)

    index = {
        key: tuple(sorted(matches, key=lambda record: record.compound_name))
        for key, matches in grouped.items()
    }

    return DILIrankTable(
        dataset_label=dataset_label,
        source_path=str(path),
        records=tuple(records),
        index=index,
        _text_scan_re=_build_text_scan_re(tuple(index)),
    )


@lru_cache(maxsize=4)
def _load_cached(path_text: str, fingerprint: tuple[int, int]) -> DILIrankTable:
    del fingerprint  # only part of the cache key
    return build_dilirank_table(Path(path_text))


def load_dilirank_table(path: Path | str | None = None) -> DILIrankTable:
    """Load the workbook, reusing the parse until the file changes on disk."""

    resolved = Path(path).expanduser() if path else default_dilirank_path()
    resolved = resolved.resolve()
    if not resolved.exists():
        raise DILIrankError(f"DILIrank workbook not found: {resolved}")
    stat = resolved.stat()
    return _load_cached(str(resolved), (stat.st_mtime_ns, stat.st_size))


__all__ = [
    "DILIRANK_CONCERN_SCORES",
    "DILIrankError",
    "DILIrankRecord",
    "DILIrankTable",
    "build_dilirank_table",
    "default_dilirank_path",
    "load_dilirank_table",
]
