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
from typing import get_args

from dili_rucam_agents.crew.tasks import (
    DEFAULT_RUCAM_INFERRING_PROMPT_PATH,
    DEFAULT_RUCAM_STRICT_PROMPT_PATH,
)
from dili_rucam_agents.hepatotoxicity.dailymed import DAILYMED_SECTION_SCORES
from dili_rucam_agents.hepatotoxicity.dilirank import DILIRANK_CONCERN_SCORES
from dili_rucam_agents.validators.rucam_assessment import (
    CauseStatus,
    DataStatus,
    EvidenceRef,
    ItemFiveCauses,
    ItemScore,
    PreFlight,
    RRatioBasis,
    RucamAssessment,
)
from dili_rucam_agents.validators.rucam_json import ITEM_LEGAL_SCORES, InjuryPattern

RULES_VERSION = "RUCAM_DananBenichou1993_modified_sheet_v3"
# A tag is retired, never reused: a score still carries the rules that produced
# it, so a report tagged with one of these predates a change in those rules.
RETIRED_RULES_VERSIONS = (
    "RUCAM_DananBenichou1993_modified_sheet_v1",
    "RUCAM_DananBenichou1993_modified_sheet_v2",
)
_SHARED_CORE_RE = re.compile(
    r"<!-- BEGIN SHARED RUCAM CORE v3.*?<!-- END SHARED RUCAM CORE v3 -->",
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
        for retired in RETIRED_RULES_VERSIONS:
            assert retired not in text, f"{name} still mentions {retired}"


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


def _output_region(text: str) -> str:
    """Everything after the frozen core: the model-facing contract and workflow."""
    return text.split("<!-- END SHARED RUCAM CORE v3 -->", 1)[1]


def _requested_object(text: str) -> dict:
    blocks = re.findall(r"```json\n(.*?)\n```", text, re.DOTALL)
    assert len(blocks) == 1, "exactly one requested-object template"
    return json.loads(blocks[0])


def test_section_c_template_matches_the_assessment_schema():
    """The object the prompts request is the object RucamAssessment accepts."""
    item_fields = set(ItemScore.model_fields)
    for name, text in _prompt_texts().items():
        template = _requested_object(text)

        assert set(template) == set(RucamAssessment.model_fields), name
        # Enumerated placeholders must list exactly the values the schema accepts.
        def options(value: str) -> set[str]:
            return {part.strip() for part in value.split("|")}

        assert options(template["injury_pattern"]) == set(
            get_args(InjuryPattern)
        ), name
        for field, status in template["item_5_causes"].items():
            if field != "g1":
                assert options(status) == {s.value for s in CauseStatus}, name
        for item in ITEM_LEGAL_SCORES:
            assert options(template[item]["data_status"]) == {
                s.value for s in DataStatus
            }, name
        # ALP of 0 is rejected by the schema, so the placeholder must not be 0.
        assert template["r_ratio_basis"]["alp"] > 0, name
        assert set(template["pre_flight"]) == set(PreFlight.model_fields), name
        assert set(template["item_5_causes"]) == set(ItemFiveCauses.model_fields), name
        assert set(template["r_ratio_basis"]) == set(RRatioBasis.model_fields), name
        for item in ITEM_LEGAL_SCORES:
            assert set(template[item]) == item_fields, f"{name}: {item}"
            (quote,) = template[item]["evidence"]
            # The model never certifies a quote; code does.
            assert set(quote) == set(EvidenceRef.model_fields) - {"verified"}, name

        # The rules tag is a constant the renderer supplies, not a model field.
        assert f"rules_version: **{RULES_VERSION}**" in text, name
        assert "rules_version" not in template, name


def test_prompts_no_longer_ask_for_a_total_or_a_category():
    """Code sums the items and derives the category; the model supplies neither."""
    for name, text in _prompt_texts().items():
        region = _output_region(text)
        template = _requested_object(text)
        for computed in ("total_score", "category"):
            assert computed not in template, f"{name} requests {computed}"
        assert '"total_score"' not in region, name
        assert '"category"' not in region, name
        assert "## SECTION A" not in region, name
        assert "## SECTION B" not in region, name
        assert "## SECTION C" not in region, name
        assert "Produce **three sections**" not in region, name
        # The core still says the contract needs three sections; this sentence is
        # the only thing that supersedes it, so its absence must fail loudly.
        assert (
            "This section is the whole output contract. It supersedes every "
            "instruction above\nabout how to emit the report, including any "
            "statement that the contract requires\nthree sections: emit no "
            "headings, no table and no `notes` array."
        ) in region, name
        for bridge in (
            'Wherever the rules above say "SECTION A"',
            "put it in the `inference_note` of the item concerned",
            'they say "SECTION B" or "SECTION C"',
        ):
            assert bridge in region, f"{name} lost the mapping clause {bridge!r}"


def test_pre_flight_check_covers_the_item_6_source():
    for name, text in _prompt_texts().items():
        assert "answer all eleven lines" in text, name
        assert "ten-line" not in text, name
        assert "6. Item 6 `tier` and `score` as read from the evidence block" in text, name
        template = _requested_object(text)
        # One numbered line per structured pre-flight answer, item 6 included.
        assert len(template["pre_flight"]) == 11, name
        assert "item_6_source" in template["pre_flight"], name
        numbered = re.findall(
            r"^(\d+)\. ", text.split("## 4. Pre-flight check", 1)[1], re.MULTILINE
        )
        assert numbered == [str(n) for n in range(1, 12)], name


def test_every_scoring_item_declares_its_allowed_values():
    """D8 is what stops an analyst inventing an off-sheet score."""
    for name, text in _prompt_texts().items():
        for item in ITEM_LEGAL_SCORES:
            assert f"| {item} |" in text, f"{name} omits {item} from the D8 table"


def test_d8_table_matches_the_code_constant():
    """D8 stays literal text; this fails when it and ITEM_LEGAL_SCORES drift."""
    row = re.compile(r"^\| (\w+) \| ([+\-\d, ]+) \|$", re.MULTILINE)
    for name, text in _prompt_texts().items():
        d8 = text.split("### D8 -- Allowed score per item", 1)[1].split("###", 1)[0]
        documented = {
            item: frozenset(int(v) for v in values.split(","))
            for item, values in row.findall(d8)
        }
        assert documented == ITEM_LEGAL_SCORES, name


def test_boundary_conventions_and_worked_cases_are_present():
    for name, text in _prompt_texts().items():
        for convention in (f"### D{index} --" for index in range(1, 9)):
            assert convention in text, f"{name} omits {convention}"
        assert "APPENDIX B -- WORKED BOUNDARY CASES" in text, name
        # The boundary case that motivated v2: a 50% fall on day 8 exactly.
        assert "day 8 <= 8 -> **+3**" in text, name
        assert "day 9 > 8 -> **+2**" in text, name


def test_latency_days_are_numbered_from_the_dose_day():
    """Day 1 is the day of the dose, so a same-day onset lands on a rung.

    Under the former day-0 anchor, an onset hours after the dose computed to
    day 0, which appears on no printed rung of item 1. Analysts split 0/+1/+2
    on one such case, and the ones scoring 0 said so: day 0 matched no row.
    Numbering latency from 1 closes that gap. The lowercase `days` durations of
    items 2 and 4 are anchored on the peak and on `onset_date`, not on a dose,
    so this rule deliberately leaves them alone.
    """

    for name, text in _prompt_texts().items():
        assert "is numbered from 1. The calendar day" in text, name
        assert "of the dose is Day 1**" in text, name
        assert "`Day = (later_date - earlier_date) + 1`" in text, name
        assert "There is no day 0" in text, name
        for rung in ("| Day 5-90 |", "| Day 1-15 |", "| Day 1-90 |"):
            assert rung in text, f"{name} omits the {rung} rung of item 1"
        # The same-day onset that motivated the change, on both treatment rows.
        assert "`onset_day` = 1, inside Day 1-4 -> **+1**" in text, name
        assert "`onset_day` = 1, inside Day 1-15 -> **+2**" in text, name
        # Item 2 keeps its elapsed clock from the peak.
        assert "`days(t) = t - t0`" in text, name
        # The Day number is printed, so a run can be audited for it.
        assert "`onset_day = N`" in text, name


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

