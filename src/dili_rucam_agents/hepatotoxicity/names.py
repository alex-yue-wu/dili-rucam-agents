"""Deterministic drug-name normalisation shared by every evidence source.

Case reports name a drug the way a clinician writes it ("amiodarone"), while
reference tables name it the way a label does ("Amiodarone hydrochloride").
Every lookup in this package therefore goes through the same two keys: the
normalised name, and the normalised name with trailing salt or hydrate tokens
removed. Both are pure string transforms so the same input always yields the
same keys.
"""

from __future__ import annotations

import re

#: Trailing tokens that name a salt, ester or hydrate rather than the moiety.
#: "acid" is deliberately absent -- it is part of the drug name in
#: "Valproic acid" and "Mycophenolic acid", not a salt suffix.
SALT_SUFFIXES = frozenset(
    {
        "acetate",
        "anhydrous",
        "aspartate",
        "benzoate",
        "besylate",
        "bitartrate",
        "bromide",
        "calcium",
        "camsylate",
        "chloride",
        "citrate",
        "dichloride",
        "dihydrate",
        "dihydrochloride",
        "dimaleate",
        "dimeglumine",
        "dipropionate",
        "disodium",
        "edisylate",
        "embonate",
        "fumarate",
        "furoate",
        "gluconate",
        "hbr",
        "hcl",
        "glucuronate",
        "hyclate",
        "hydrobromide",
        "hydrochloride",
        "lactate",
        "malate",
        "maleate",
        "meglumine",
        "mesylate",
        "monohydrate",
        "napsylate",
        "nitrate",
        "oxalate",
        "palmitate",
        "pamoate",
        "phosphate",
        "potassium",
        "propionate",
        "sodium",
        "stearate",
        "succinate",
        "sulfate",
        "sulphate",
        "tartrate",
        "tosylate",
        "tromethamine",
        "trisodium",
        "valerate",
        "xinafoate",
    }
)

_PUNCTUATION_RE = re.compile(r"[^a-z0-9\s-]+")
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_drug_name(raw: str) -> str:
    """Lower-case `raw`, drop punctuation and collapse whitespace."""

    lowered = str(raw).strip().lower()
    lowered = lowered.replace(",", " ").replace("/", " ")
    lowered = _PUNCTUATION_RE.sub(" ", lowered)
    return _WHITESPACE_RE.sub(" ", lowered).strip()


def strip_salt_suffixes(normalized: str) -> str:
    """Remove trailing salt/hydrate tokens, never the final remaining token."""

    tokens = normalized.split()
    while len(tokens) > 1 and tokens[-1] in SALT_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def drug_name_keys(raw: str) -> tuple[str, ...]:
    """Return the lookup keys for `raw`, most specific first, without blanks."""

    normalized = normalize_drug_name(raw)
    if not normalized:
        return ()
    stripped = strip_salt_suffixes(normalized)
    if stripped == normalized:
        return (normalized,)
    # "Sodium lactate" strips to the bare cation "sodium", which would match
    # every saline infusion in a case report. A key that is nothing but a salt
    # token identifies no drug, so it is not offered.
    if stripped in SALT_SUFFIXES:
        return (normalized,)
    return (normalized, stripped)


__all__ = [
    "SALT_SUFFIXES",
    "drug_name_keys",
    "normalize_drug_name",
    "strip_salt_suffixes",
]
