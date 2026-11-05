"""Deterministic drug-name normalisation shared by every evidence source.

A case report names a drug the way its authors write it; a reference table names
it the way a US label does. Those differ in four ways, and all four are handled
here so that one spelling in a paper reaches the right row:

- salt, hydrate and formulation words ("Amiodarone hydrochloride")
- British spellings ("sulphasalazine", "oestradiol")
- INN against USAN names ("paracetamol", "ciclosporin", "rifampicin")
- word order for salts written first ("sodium valproate")

Every transform is a pure string operation, so one input always yields the same
ordered set of keys. `drug_name_keys` expands a name being looked up;
`drug_name_index_keys` additionally returns the alias spellings a reference row
should be findable under, which is what lets a scan of the case text match
"paracetamol" against the Acetaminophen row.
"""

from __future__ import annotations

import re
from typing import Mapping

#: Trailing tokens naming a salt, ester, hydrate or formulation rather than the
#: moiety. "acid" is deliberately absent -- it is part of the drug name in
#: "Valproic acid" and "Mycophenolic acid", not a suffix.
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
        "glucuronate",
        "hbr",
        "hcl",
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

#: Trailing tokens naming how the product is made or presented. Stripping these
#: is what lets "tetracycline" reach "Tetracycline phosphate complex".
FORMULATION_SUFFIXES = frozenset(
    {
        "complex",
        "human",
        "kit",
        "ovine",
        "recombinant",
        "resin",
        "synthetic",
    }
)

STRIPPABLE_SUFFIXES = SALT_SUFFIXES | FORMULATION_SUFFIXES

#: Cations that may be written first in British usage ("sodium valproate").
LEADING_SALT_TOKENS = frozenset(
    {"calcium", "disodium", "magnesium", "potassium", "sodium", "zinc"}
)

#: Alternative names as published, mapped to the form US reference data uses.
#: Only unambiguous one-to-one equivalences belong here: a wrong entry would
#: silently score a different drug. Combination products (co-amoxiclav,
#: co-trimoxazole) are deliberately absent -- they are not synonyms.
DRUG_NAME_ALIASES: Mapping[str, str] = {
    "adrenaline": "epinephrine",
    "amethocaine": "tetracaine",
    "amoxycillin": "amoxicillin",
    "amylobarbitone": "amobarbital",
    "beclometasone": "beclomethasone",
    "benzylpenicillin": "penicillin g",
    "cefalexin": "cephalexin",
    "cefradine": "cephradine",
    "chlorphenamine": "chlorpheniramine",
    "chlortalidone": "chlorthalidone",
    "ciclosporin": "cyclosporine",
    "clomifene": "clomiphene",
    "colecalciferol": "cholecalciferol",
    "cyclosporin": "cyclosporine",
    "dexamfetamine": "dextroamphetamine",
    "dicoumarol": "dicumarol",
    "ethinyloestradiol": "ethinyl estradiol",
    "ethinylestradiol": "ethinyl estradiol",
    "frusemide": "furosemide",
    "glibenclamide": "glyburide",
    "guaiphenesin": "guaifenesin",
    "hydroxycarbamide": "hydroxyurea",
    "indometacin": "indomethacin",
    "isoprenaline": "isoproterenol",
    "lignocaine": "lidocaine",
    "mesalazine": "mesalamine",
    "noradrenaline": "norepinephrine",
    "norethisterone": "norethindrone",
    "paracetamol": "acetaminophen",
    "pethidine": "meperidine",
    "phenobarbitone": "phenobarbital",
    "phenoxymethylpenicillin": "penicillin v",
    "quinalbarbitone": "secobarbital",
    "rifampicin": "rifampin",
    "salbutamol": "albuterol",
    "simeticone": "simethicone",
    "sodium valproate": "valproic acid",
    "tetracosactide": "cosyntropin",
    "thiopentone": "thiopental",
    "thyroxine": "levothyroxine",
    "valproate": "valproic acid",
    "valproate sodium": "valproic acid",
}

#: Spelling differences applied in both directions, so neither side of a lookup
#: has to be the canonical one. A rule that produces a name no reference table
#: holds simply yields a key that never matches.
_SPELLING_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"sulph"), "sulf"),
    (re.compile(r"sulf"), "sulph"),
    (re.compile(r"\boe"), "e"),
    (re.compile(r"oe"), "e"),
    (re.compile(r"\bestr"), "oestr"),
    (re.compile(r"ycillin"), "icillin"),
    (re.compile(r"icillin"), "ycillin"),
    (re.compile(r"\bcef"), "ceph"),
    (re.compile(r"\bceph"), "cef"),
)

_PUNCTUATION_RE = re.compile(r"[^a-z0-9\s-]+")
_WHITESPACE_RE = re.compile(r"\s+")

_ALIASES_BY_CANONICAL: dict[str, tuple[str, ...]] = {}
for _alias, _canonical in sorted(DRUG_NAME_ALIASES.items()):
    _ALIASES_BY_CANONICAL[_canonical] = _ALIASES_BY_CANONICAL.get(_canonical, ()) + (
        _alias,
    )


def normalize_drug_name(raw: str) -> str:
    """Lower-case `raw`, drop punctuation and collapse whitespace."""

    lowered = str(raw).strip().lower()
    lowered = lowered.replace(",", " ").replace("/", " ")
    lowered = _PUNCTUATION_RE.sub(" ", lowered)
    return _WHITESPACE_RE.sub(" ", lowered).strip()


def strip_salt_suffixes(normalized: str) -> str:
    """Remove trailing salt, hydrate and formulation tokens.

    The final remaining token is never removed, so a name that is nothing but
    such tokens survives unchanged.
    """

    tokens = normalized.split()
    while len(tokens) > 1 and tokens[-1] in STRIPPABLE_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def reorder_leading_salt(normalized: str) -> str:
    """Turn "sodium valproate" into "valproate sodium"; leave anything else."""

    tokens = normalized.split()
    if len(tokens) == 2 and tokens[0] in LEADING_SALT_TOKENS:
        return f"{tokens[1]} {tokens[0]}"
    return normalized


def spelling_variants(normalized: str) -> tuple[str, ...]:
    """`normalized` plus each single-rule spelling variant of it."""

    variants = [normalized]
    for pattern, replacement in _SPELLING_RULES:
        candidate = pattern.sub(replacement, normalized)
        if candidate != normalized and candidate not in variants:
            variants.append(candidate)
    return tuple(variants)


def _is_bare_modifier(name: str) -> bool:
    """Whether `name` is a lone salt or formulation word, which names no drug."""

    return name in STRIPPABLE_SUFFIXES


def _expand(normalized: str) -> list[str]:
    keys: list[str] = []

    def add(candidate: str) -> None:
        if candidate and candidate not in keys and not _is_bare_modifier(candidate):
            keys.append(candidate)

    for base in (normalized, reorder_leading_salt(normalized)):
        for spelled in spelling_variants(base):
            add(spelled)
            stripped = strip_salt_suffixes(spelled)
            add(stripped)
            for candidate in (spelled, stripped):
                alias_target = DRUG_NAME_ALIASES.get(candidate)
                if alias_target:
                    add(alias_target)
                    add(strip_salt_suffixes(alias_target))
    return keys


def drug_name_keys(raw: str) -> tuple[str, ...]:
    """Lookup keys for `raw`, most specific first, without blanks.

    The exact normalised name comes first, then the salt-stripped form, then
    spelling and synonym variants, so an exact match always wins over a variant.
    """

    normalized = normalize_drug_name(raw)
    if not normalized:
        return ()
    return tuple(_expand(normalized))


def drug_name_index_keys(raw: str) -> tuple[str, ...]:
    """Keys a reference row named `raw` should be findable under.

    Everything `drug_name_keys` returns, plus the published alternatives for it,
    so scanning case text for reference names also matches "paracetamol",
    "ciclosporin" or "sulphasalazine".
    """

    keys = list(drug_name_keys(raw))
    for key in tuple(keys):
        for alias in _ALIASES_BY_CANONICAL.get(key, ()):
            for variant in spelling_variants(alias):
                if variant not in keys and not _is_bare_modifier(variant):
                    keys.append(variant)
    return tuple(keys)


__all__ = [
    "DRUG_NAME_ALIASES",
    "FORMULATION_SUFFIXES",
    "LEADING_SALT_TOKENS",
    "SALT_SUFFIXES",
    "STRIPPABLE_SUFFIXES",
    "drug_name_index_keys",
    "drug_name_keys",
    "normalize_drug_name",
    "reorder_leading_salt",
    "spelling_variants",
    "strip_salt_suffixes",
]
