"""Guards that keep the RUCAM scoring rules identical across prompt variants.

The strict and inferring prompts each embed the full scoring sheet so that a
single file is all an analyst ever receives. That duplication is only safe while
the shared region stays byte-identical, so these tests fail loudly when one copy
is edited on its own, and when the rules drift away from the output contract the
validators enforce.
"""

from __future__ import annotations

import json
import re

from dili_rucam_agents.crew.tasks import (
    DEFAULT_RUCAM_INFERRING_PROMPT_PATH,
    DEFAULT_RUCAM_STRICT_PROMPT_PATH,
)
from dili_rucam_agents.hepatotoxicity.dailymed import DAILYMED_SECTION_SCORES
from dili_rucam_agents.hepatotoxicity.dilirank import DILIRANK_CONCERN_SCORES
from dili_rucam_agents.validators.rucam_json import RucamReport, RucamScores

RULES_VERSION = "RUCAM_DananBenichou1993_modified_sheet_v2"
_SHARED_CORE_RE = re.compile(
    r"<!-- BEGIN SHARED RUCAM CORE v2.*?<!-- END SHARED RUCAM CORE v2 -->",
    re.DOTALL,
)
_PROMPT_PATHS = (
    DEFAULT_RUCAM_STRICT_PROMPT_PATH,
    DEFAULT_RUCAM_INFERRING_PROMPT_PATH,
)


def _prompt_texts() -> dict[str, str]:
    return {path.name: path.read_text(encoding="utf-8") for path in _PROMPT_PATHS}


def _shared_cores() -> dict[str, str]:
    cores = {}
    for name, text in _prompt_texts().items():
        matches = _SHARED_CORE_RE.findall(text)
        assert len(matches) == 1, f"{name} must hold exactly one shared core region"
        cores[name] = matches[0]
    return cores


def test_shared_rules_core_is_byte_identical_across_prompt_variants():
    cores = _shared_cores()

    assert len(set(cores.values())) == 1, (
        "The shared RUCAM core drifted between prompt variants. Edit the region in "
        "every prompt together, not one file at a time."
    )


def test_both_prompts_pin_the_same_rules_version():
    for name, text in _prompt_texts().items():
        assert f"rules_version: **{RULES_VERSION}**" in text, name
        assert f'"rules_version": "{RULES_VERSION}"' in text, name
        # A second, competing version tag is what made v1 unreproducible.
        assert "RUCAM_ULN_LiverTox_v1" not in text, name
        assert "RUCAM_DananBenichou1993_modified_sheet_v1" not in text, name


def test_r_ratio_boundaries_are_stated_once_and_without_contradiction():
    """R = 2.00 and R = 5.00 must land in exactly one pattern per prompt."""
    for name, text in _prompt_texts().items():
        assert "| R >= 5.00 | Hepatocellular |" in text, name
        assert "| 2.00 < R < 5.00 | Mixed |" in text, name
        assert "| R <= 2.00 | Cholestatic |" in text, name
        for contradiction in (
            "Hepatocellular: R > 5.0",
            "Cholestatic: R < 2.0",
            "Mixed: R = 2.0",
            "**R ≥ 5** → Hepatocellular",
            "**R ≤ 2** → Cholestatic",
        ):
            assert contradiction not in text, f"{name} still states {contradiction!r}"


def test_section_c_template_matches_the_validated_schema():
    """The JSON the prompts ask for must be the JSON the validator accepts."""
    for name, text in _prompt_texts().items():
        blocks = re.findall(r"```json\n(.*?)\n```", text, re.DOTALL)
        assert len(blocks) == 1, f"{name} must show exactly one SECTION C template"
        template = json.loads(
            re.sub(
                r'"([a-z_]+)": "[^"]*\|[^"]*"',
                lambda match: f'"{match.group(1)}": "cholestatic"'
                if match.group(1) == "injury_pattern"
                else f'"{match.group(1)}": "Excluded"',
                blocks[0],
            )
        )

        assert set(template) == set(RucamReport.model_fields) | {
            "rules_version",
            "notes",
        }, name
        assert set(template["rucam_scores"]) == set(RucamScores.model_fields), name
        assert template["rules_version"] == RULES_VERSION, name
        assert template["notes"] == [], name


def test_every_scoring_item_declares_its_allowed_values():
    """D8 is what stops an analyst inventing an off-sheet score."""
    for name, text in _prompt_texts().items():
        for item in RucamScores.model_fields:
            assert f"| {item} |" in text, f"{name} omits {item} from the D8 table"


def test_boundary_conventions_and_worked_cases_are_present():
    for name, text in _prompt_texts().items():
        for convention in (f"### D{index} --" for index in range(1, 9)):
            assert convention in text, f"{name} omits {convention}"
        assert "APPENDIX B -- WORKED BOUNDARY CASES" in text, name
        # The boundary case that motivated v2: a 50% fall on day 8 exactly.
        assert "day 8 <= 8 -> **+3**" in text, name
        assert "day 9 > 8 -> **+2**" in text, name


def test_percentage_fall_formula_is_anchored_to_the_peak_to_uln_distance():
    for name, text in _prompt_texts().items():
        assert "percent_fall(t) = (peak - value(t)) / (peak - ULN) * 100" in text, name


def test_alt_has_a_default_uln_because_the_r_ratio_needs_one():
    """v1 forbade a guessed ALT ULN without ever supplying a default."""
    for name, text in _prompt_texts().items():
        assert "| ALT | 33 U/L | 25 U/L | 40 U/L |" in text, name


def test_strict_prompt_forbids_qualitative_inference():
    strict = DEFAULT_RUCAM_STRICT_PROMPT_PATH.read_text(encoding="utf-8")

    assert "# SCORING MODE -- STRICT (no qualitative inference)" in strict
    assert "Course inferred from qualitative description" not in strict


def test_inferring_prompt_bounds_its_inferences():
    inferring = DEFAULT_RUCAM_INFERRING_PROMPT_PATH.read_text(encoding="utf-8")

    assert "# SCORING MODE -- INFERRING (bounded qualitative inference)" in inferring
    assert "**Never** infer +3." in inferring
    assert "Course inferred from qualitative description" in inferring


def test_item_6_defers_to_the_evidence_cascade():
    """Item 6 was the one rung an analyst could answer from model memory."""

    for name, text in _prompt_texts().items():
        assert "## APPENDIX C -- THE ITEM 6 EVIDENCE BLOCK" in text, name
        for heading in (
            "### C.1 What the block is",
            "### C.2 Matching an entry to your suspect drug",
            "### C.3 How each source maps to points",
            "### C.4 Fallback rungs",
        ):
            assert heading in text, f"{name} omits {heading}"
        assert "**Do not answer this item from memory.**" in text, name
        assert "`case_bundle_json.hepatotoxicity_evidence`" in text, name
        for tier in (
            "dilirank_v2",
            "dailymed_label",
            "pubmed_case_reports",
            "unresolved",
        ):
            assert tier in text, f"{name} omits tier {tier}"
        # The retired rung let the analyst award +1 from recall alone.
        assert "established in the published literature to" not in text, name


def test_prompt_score_tables_match_the_resolver_constants():
    """A mapping change in code must be mirrored in the prompt, and vice versa."""

    concern_row = re.compile(r"^\| (v\w+-DILI-concern|Ambiguous-DILI-concern) \| \*\*([+-]?\d)\*\* \|$", re.MULTILINE)
    section_row = re.compile(r"^\| (Boxed warning|Warnings & precautions|Adverse reactions) \| \*\*([+-]?\d)\*\* \|$", re.MULTILINE)
    section_keys = {
        "Boxed warning": "boxed_warning",
        "Warnings & precautions": "warnings_and_precautions",
        "Adverse reactions": "adverse_reactions",
    }

    for name, text in _prompt_texts().items():
        documented_concerns = {
            concern.lower(): int(points) for concern, points in concern_row.findall(text)
        }
        assert documented_concerns == dict(DILIRANK_CONCERN_SCORES), name

        documented_sections = {
            section_keys[label]: int(points) for label, points in section_row.findall(text)
        }
        assert documented_sections == dict(DAILYMED_SECTION_SCORES), name


def test_pre_flight_check_covers_the_item_6_source():
    for name, text in _prompt_texts().items():
        assert "answer all eleven lines" in text, name
        assert "6. Item 6 `tier` and `score` as read from the evidence block" in text, name
