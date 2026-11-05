"""Tests for the RUCAM item 6 evidence cascade.

No test here opens a socket. The DailyMed and PubMed tiers take their fetchers
as arguments, so every networked path is driven from fixtures, and a fetcher
that must not be reached raises instead of returning.
"""

from __future__ import annotations

import json

from openpyxl import Workbook
import pytest

from dili_rucam_agents.hepatotoxicity import dailymed, literature
from dili_rucam_agents.hepatotoxicity.cache import EvidenceCache, cache_key
from dili_rucam_agents.hepatotoxicity.dilirank import (
    DILIRANK_CONCERN_SCORES,
    DILIrankError,
    build_dilirank_table,
    default_dilirank_path,
    load_dilirank_table,
)
from dili_rucam_agents.hepatotoxicity.evidence import (
    BUNDLE_KEY,
    TIER_DAILYMED,
    TIER_DILIRANK,
    TIER_LITERATURE,
    TIER_UNRESOLVED,
    EvidenceResolver,
    annotate_case_bundle_payload,
    resolve_case_evidence,
)
from dili_rucam_agents.hepatotoxicity.names import (
    drug_name_index_keys,
    drug_name_keys,
    strip_salt_suffixes,
)
from dili_rucam_agents.hepatotoxicity.report import render_hepatotoxicity_report
from dili_rucam_agents.hepatotoxicity.settings import (
    HepatotoxicityEvidenceSettings,
    apply_hepatotoxicity_evidence,
)
from dili_rucam_agents.hepatotoxicity.transport import FetchError, build_url

AS_OF = "2026-11-04"

SPL_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<document xmlns="urn:hl7-org:v3">
  <component><structuredBody>
    {sections}
  </structuredBody></component>
</document>
"""

SECTION_TEMPLATE = (
    "<component><section><code code=\"{code}\"/><text>{body}</text></section></component>"
)


def spl(*sections: tuple[str, str]) -> str:
    return SPL_TEMPLATE.format(
        sections="".join(
            SECTION_TEMPLATE.format(code=code, body=body) for code, body in sections
        )
    )


def unreachable_fetcher(url: str):
    raise AssertionError(f"the network must not be reached, but {url} was requested")


def write_workbook(path, rows):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "version 2"
    sheet.append(["Drug Induced Liver Injury Rank (DILIrank) Dataset Ver 2.0 | FDA"])
    sheet.append(
        ["LTKBID", "CompoundName", "SeverityClass", "LabelSection", "vDILI-Concern", "Comment"]
    )
    for row in rows:
        sheet.append(list(row))
    workbook.save(path)
    return path


@pytest.fixture
def table(tmp_path):
    return build_dilirank_table(
        write_workbook(
            tmp_path / "dilirank.xlsx",
            [
                ("LT1", "Amiodarone hydrochloride", 8, "Box warning", "vMost-DILI-concern"),
                ("LT2", "Amoxicillin", 3, "Adverse reactions", "vLess-DILI-concern"),
                ("LT3", "Metoprolol succinate", 3, "Adverse reactions", "Ambiguous-DILI-concern"),
                ("LT4", "Metoprolol tartrate", 3, "Adverse reactions", "vLess-DILI-concern"),
                ("LT5", "Abaloparatide", 0, "No match", "vNo-DILI-concern"),
                ("LT6", "Valproic acid", 8, "Box warning", "vMOST-DILI-concern"),
                ("LT7", "Sodium lactate", 0, "No match", "vNo-DILI-concern"),
            ],
        )
    )


# --------------------------------------------------------------------------- names


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Amiodarone hydrochloride", ("amiodarone hydrochloride", "amiodarone")),
        ("Amiodarone HCl", ("amiodarone hcl", "amiodarone")),
        ("Valproic acid", ("valproic acid",)),
        ("  ", ()),
    ],
)
def test_drug_name_keys_offer_the_salt_stripped_form(raw, expected):
    assert drug_name_keys(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("sulphasalazine", "sulfasalazine"),
        ("oestradiol", "estradiol"),
        ("amoxycillin", "amoxicillin"),
        ("cefalexin", "cephalexin"),
        ("paracetamol", "acetaminophen"),
        ("ciclosporin", "cyclosporine"),
        ("rifampicin", "rifampin"),
        ("frusemide", "furosemide"),
        ("sodium valproate", "valproic acid"),
        ("Tetracycline phosphate complex", "tetracycline"),
        ("Morphine sulphate", "morphine sulfate"),
    ],
)
def test_published_spellings_reach_the_us_reference_form(raw, expected):
    """British spellings, INN names and formulation words must all resolve."""

    assert expected in drug_name_keys(raw)


def test_the_exact_name_is_always_tried_before_any_variant():
    keys = drug_name_keys("Morphine sulphate")

    assert keys[0] == "morphine sulphate"
    assert keys.index("morphine sulphate") < keys.index("morphine")


def test_a_reference_row_is_indexed_under_its_published_alternatives():
    keys = drug_name_index_keys("Acetaminophen")

    assert "acetaminophen" in keys and "paracetamol" in keys


def test_a_bare_cation_is_never_offered_as_a_lookup_key():
    """"Sodium lactate" must not be findable as "sodium"."""

    assert strip_salt_suffixes("sodium lactate") == "sodium"
    assert "sodium" not in drug_name_keys("Sodium lactate")
    assert "lactate" not in drug_name_keys("Sodium lactate")


# ------------------------------------------------------------------------ dilirank


def test_pinned_workbook_loads_and_covers_every_concern_class():
    pinned = load_dilirank_table()

    assert len(pinned.records) > 1000
    assert "DILIrank" in pinned.dataset_label
    unmapped = {
        record.concern for record in pinned.records if record.score is None
    }
    assert not unmapped, (
        "DILIrank holds concern classes the score mapping does not cover: "
        f"{sorted(unmapped)}"
    )


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Amiodarone hydrochloride", 2),
        ("amiodarone", 2),
        ("Amoxicillin", 2),
        ("Metoprolol succinate", 1),
        ("Abaloparatide", 0),
        ("Valproic acid", 2),
    ],
)
def test_concern_class_maps_to_item_6_points(table, name, expected):
    records = table.lookup(name)

    assert records and records[0].score == expected


def test_concern_class_comparison_ignores_case():
    assert DILIRANK_CONCERN_SCORES["vmost-dili-concern"] == 2


def test_text_scan_matches_across_a_line_break_and_skips_saline(table):
    hits = dict(
        table.find_in_text(
            "Started amoxicillin, then valproic\nacid; sodium chloride infusion given."
        )
    )

    assert set(hits) == {"amoxicillin", "valproic acid"}


def test_missing_workbook_is_reported(tmp_path):
    with pytest.raises(DILIrankError, match="not found"):
        load_dilirank_table(tmp_path / "absent.xlsx")


def test_workbook_without_the_expected_columns_is_reported(tmp_path):
    workbook = Workbook()
    workbook.active.title = "version 2"
    workbook.active.append(["title"])
    workbook.active.append(["LTKBID", "CompoundName"])
    workbook.active.append(["LT1", "Aspirin"])
    path = tmp_path / "broken.xlsx"
    workbook.save(path)

    with pytest.raises(DILIrankError, match="missing expected columns"):
        build_dilirank_table(path)


# ------------------------------------------------------------------------ dailymed


def test_spl_search_is_ordered_by_setid_not_publication_date():
    payload = {
        "data": [
            {"setid": "zzz", "title": "Z", "spl_version": 2, "published_date": "Nov 2026"},
            {"setid": "aaa", "title": "A", "spl_version": 1, "published_date": "Jan 2020"},
            {"title": "no setid"},
        ],
        "metadata": {"total_elements": 7},
    }

    references, total = dailymed.parse_spl_search(payload)

    assert [reference.setid for reference in references] == ["aaa", "zzz"]
    assert total == 7


@pytest.mark.parametrize(
    ("code", "section"),
    [
        ("34066-1", "boxed_warning"),
        ("43685-7", "warnings_and_precautions"),
        ("34071-1", "warnings_and_precautions"),
        ("42232-9", "warnings_and_precautions"),
        ("34084-4", "adverse_reactions"),
    ],
)
def test_each_label_section_code_is_recognised(code, section):
    classification = dailymed.classify_spl_xml(
        spl((code, "Fatal hepatotoxicity has been reported."))
    )

    assert classification.sections == (section,)
    assert "hepatotoxicity" in classification.matched_terms


def test_pre_existing_liver_disease_is_not_a_reported_reaction():
    """"Use caution in hepatic impairment" is a dosing caveat, not an injury."""

    classification = dailymed.classify_spl_xml(
        spl(("43685-7", "Use caution in patients with hepatic impairment."))
    )

    assert classification.sections == ()


def test_unparsable_label_classifies_as_nothing():
    assert dailymed.classify_spl_xml("<not xml").sections == ()


def test_strongest_section_decides_the_score():
    finding = dailymed.resolve_dailymed(
        "Fakedrug",
        fetch_json=lambda _: {
            "data": [{"setid": "aaa", "title": "FAKEDRUG", "spl_version": 1,
                      "published_date": "Jan 2026"}],
            "metadata": {"total_elements": 1},
        },
        fetch_text=lambda _: spl(
            ("34084-4", "Jaundice reported."),
            ("34066-1", "Fatal liver failure."),
        ),
    )

    assert finding.section == "boxed_warning"
    assert finding.score == 2


def test_adverse_reactions_alone_scores_one():
    finding = dailymed.resolve_dailymed(
        "Fakedrug",
        fetch_json=lambda _: {
            "data": [{"setid": "aaa"}],
            "metadata": {"total_elements": 1},
        },
        fetch_text=lambda _: spl(("34084-4", "Cholestasis has been reported.")),
    )

    assert finding.score == 1


def test_no_label_is_distinguished_from_a_silent_label():
    finding = dailymed.resolve_dailymed(
        "Fakedrug",
        fetch_json=lambda _: {"data": [], "metadata": {"total_elements": 0}},
        fetch_text=unreachable_fetcher,
    )

    assert finding.label_found is False
    assert finding.score is None


def test_dailymed_finding_survives_a_cache_round_trip():
    original = dailymed.resolve_dailymed(
        "Fakedrug",
        fetch_json=lambda _: {
            "data": [{"setid": "aaa", "title": "FAKEDRUG", "spl_version": 3,
                      "published_date": "Jan 2026"}],
            "metadata": {"total_elements": 4},
        },
        fetch_text=lambda _: spl(("34066-1", "Hepatic failure.")),
    )

    restored = dailymed.finding_from_cache("Fakedrug", original.to_dict())

    assert restored == original


# ---------------------------------------------------------------------- literature


def test_pubmed_query_is_bounded_by_the_as_of_date():
    query = literature.build_pubmed_query("Tolvaptan", as_of=AS_OF)

    assert '"Tolvaptan"[Title/Abstract]' in query
    assert '"2026/11/04"[PDAT]' in query
    assert "case reports" in query


@pytest.mark.parametrize("bad", ["2026-13-01", "04/11/2026", "", "2026-02-31"])
def test_an_unusable_as_of_date_is_rejected(bad):
    with pytest.raises(ValueError):
        literature.build_pubmed_query("Tolvaptan", as_of=bad)


def test_esearch_count_and_ids_are_read():
    assert literature.parse_esearch(
        {"esearchresult": {"count": "12", "idlist": ["1", "2"]}}
    ) == (12, ("1", "2"))
    assert literature.parse_esearch({"unexpected": True}) == (0, ())


@pytest.mark.parametrize(("count", "expected"), [(0, 0), (1, 1), (9, 1)])
def test_one_published_case_report_is_enough(count, expected):
    finding = literature.resolve_literature(
        "Fakedrug",
        as_of=AS_OF,
        fetch_json=lambda _: {"esearchresult": {"count": str(count), "idlist": []}},
    )

    assert finding.score == expected


# --------------------------------------------------------------------------- cache


def test_cache_round_trips_through_a_file(tmp_path):
    path = tmp_path / "cache.json"
    cache = EvidenceCache.load(path, clock=lambda: "2026-01-01T00:00:00Z")
    cache.put(cache_key("dailymed", "Fakedrug", AS_OF), {"section": "boxed_warning"})

    assert cache.save() is True
    reloaded = EvidenceCache.load(path)
    entry = reloaded.get(cache_key("dailymed", "fakedrug ", AS_OF))
    assert entry == {"section": "boxed_warning", "fetched_at": "2026-01-01T00:00:00Z"}


def test_a_corrupt_cache_file_is_treated_as_empty(tmp_path):
    path = tmp_path / "cache.json"
    path.write_text("{not json")

    assert EvidenceCache.load(path).entries == {}


def test_nothing_is_written_when_no_entry_was_added(tmp_path):
    path = tmp_path / "cache.json"

    assert EvidenceCache.load(path).save() is False
    assert not path.exists()


# ------------------------------------------------------------------------ cascade


def test_dilirank_resolves_without_touching_the_network(table):
    resolver = EvidenceResolver(
        table=table,
        as_of=AS_OF,
        fetch_json=unreachable_fetcher,
        fetch_text=unreachable_fetcher,
    )

    finding = resolver.resolve("amiodarone")

    assert (finding.tier, finding.score) == (TIER_DILIRANK, 2)
    assert finding.matched_names == ("Amiodarone hydrochloride",)
    assert "vMost-DILI-concern" in finding.rationale


def test_one_name_matching_two_rows_takes_the_lower_score(table):
    resolver = EvidenceResolver(table=table, as_of=AS_OF, fetch_json=unreachable_fetcher,
                               fetch_text=unreachable_fetcher)

    finding = resolver.resolve("metoprolol")

    assert finding.score == 1
    assert finding.matched_names == ("Metoprolol succinate", "Metoprolol tartrate")
    assert any("D4" in note for note in finding.notes)


def test_offline_mode_leaves_an_unlisted_drug_unresolved(table):
    resolver = EvidenceResolver(
        table=table,
        as_of=AS_OF,
        network_enabled=False,
        fetch_json=unreachable_fetcher,
        fetch_text=unreachable_fetcher,
    )

    finding = resolver.resolve("Fakedrug")

    assert (finding.tier, finding.score) == (TIER_UNRESOLVED, None)


def test_a_silent_label_falls_through_to_the_literature_tier(table):
    resolver = EvidenceResolver(
        table=table,
        as_of=AS_OF,
        fetch_json=lambda url: (
            {"data": [{"setid": "aaa"}], "metadata": {"total_elements": 1}}
            if "spls.json" in url
            else {"esearchresult": {"count": "3", "idlist": ["9"]}}
        ),
        fetch_text=lambda _: spl(("34084-4", "Headache reported.")),
    )

    finding = resolver.resolve("Fakedrug")

    assert (finding.tier, finding.score) == (TIER_LITERATURE, 1)
    assert finding.dailymed is not None and finding.dailymed["label_found"] is True
    assert any("mentions liver injury" in note for note in finding.notes)


def test_the_label_tier_wins_over_the_literature_tier(table):
    calls: list[str] = []

    def fetch_json(url):
        calls.append(url)
        return {"data": [{"setid": "aaa"}], "metadata": {"total_elements": 1}}

    resolver = EvidenceResolver(
        table=table,
        as_of=AS_OF,
        fetch_json=fetch_json,
        fetch_text=lambda _: spl(("34066-1", "Fatal hepatitis.")),
    )

    finding = resolver.resolve("Fakedrug")

    assert (finding.tier, finding.score) == (TIER_DAILYMED, 2)
    assert finding.literature is None
    assert not [url for url in calls if "esearch" in url]


def test_a_cached_answer_is_reused_instead_of_refetched(table):
    calls: list[str] = []

    resolver = EvidenceResolver(
        table=table,
        as_of=AS_OF,
        cache=EvidenceCache(),
        fetch_json=lambda url: (calls.append(url) or
                                {"data": [{"setid": "aaa"}], "metadata": {"total_elements": 1}}),
        fetch_text=lambda url: (calls.append(url) or spl(("34066-1", "Hepatic necrosis."))),
    )

    first = resolver.resolve("Fakedrug")
    fetched = len(calls)
    second = resolver.resolve("Fakedrug")

    assert fetched > 0
    assert len(calls) == fetched
    assert (first.tier, first.score) == (second.tier, second.score)


def test_an_unreachable_source_is_reported_rather_than_guessed(table):
    def failing(url):
        raise FetchError(f"boom for {url}")

    resolver = EvidenceResolver(
        table=table, as_of=AS_OF, fetch_json=failing, fetch_text=failing
    )

    finding = resolver.resolve("Fakedrug")

    assert (finding.tier, finding.score) == (TIER_UNRESOLVED, None)
    assert any("DailyMed lookup failed" in note for note in finding.notes)
    assert any("PubMed lookup failed" in note for note in finding.notes)


def test_names_found_in_the_case_text_never_reach_the_network(table):
    resolution = resolve_case_evidence(
        {"normalized_text": "Amoxicillin and amiodarone were given."},
        table=table,
        as_of=AS_OF,
        fetch_json=unreachable_fetcher,
        fetch_text=unreachable_fetcher,
    )

    assert {finding.drug_name for finding in resolution.findings} == {
        "amoxicillin",
        "amiodarone",
    }
    assert all(finding.origin == "case_bundle_text" for finding in resolution.findings)


def test_scan_covers_blocks_and_table_cells(table):
    resolution = resolve_case_evidence(
        {
            "normalized_text": "",
            "blocks": [{"text": "Amoxicillin 500 mg"}],
            "tables": [{"preview": "", "raw_rows": [["Abaloparatide", "20 mcg"]]}],
        },
        table=table,
        as_of=AS_OF,
        network_enabled=False,
    )

    assert {finding.drug_name for finding in resolution.findings} == {
        "amoxicillin",
        "abaloparatide",
    }


def test_a_requested_name_is_resolved_even_when_absent_from_the_text(table):
    resolution = resolve_case_evidence(
        {"normalized_text": "No drug names here."},
        drug_names=["Fakedrug"],
        table=table,
        as_of=AS_OF,
        network_enabled=False,
    )

    assert [finding.origin for finding in resolution.findings] == ["requested"]


def test_an_empty_resolution_says_so(table):
    resolution = resolve_case_evidence(
        {"normalized_text": "No drug names here."},
        table=table,
        as_of=AS_OF,
        network_enabled=False,
    )

    assert resolution.findings == ()
    assert any("no pre-resolved evidence" in note for note in resolution.notes)


def test_annotation_adds_the_block_and_changes_nothing_else(table):
    payload = {"normalized_text": "Amoxicillin given.", "unknowns": ["x"]}

    annotated = annotate_case_bundle_payload(
        payload, table=table, as_of=AS_OF, network_enabled=False
    )

    assert BUNDLE_KEY not in payload
    assert annotated["unknowns"] == ["x"]
    assert annotated[BUNDLE_KEY]["drugs"][0]["score"] == 2
    assert annotated[BUNDLE_KEY]["as_of"] == AS_OF
    json.dumps(annotated)  # the block has to survive serialisation to the analyst


# ------------------------------------------------------------------------ settings


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("enabled", False),
        ("drug_names", ("aspirin",)),
        ("network_enabled", False),
        ("as_of", "2025-01-01"),
        ("cache_path", "/tmp/other.json"),
        ("dilirank_path", "/tmp/other.xlsx"),
        ("max_labels", 1),
        ("min_reports", 2),
    ],
)
def test_every_setting_changes_the_fingerprint(field, value):
    base = HepatotoxicityEvidenceSettings()

    assert base.fingerprint() != HepatotoxicityEvidenceSettings(
        **{**{"enabled": True}, field: value}
    ).fingerprint()


def test_drug_name_order_does_not_change_the_fingerprint():
    first = HepatotoxicityEvidenceSettings(drug_names=("a", "b"))
    second = HepatotoxicityEvidenceSettings(drug_names=("b", "a"))

    assert first.fingerprint() == second.fingerprint()


def test_disabling_the_cascade_leaves_the_bundle_untouched():
    payload, resolution = apply_hepatotoxicity_evidence(
        {"normalized_text": "Amoxicillin given."},
        HepatotoxicityEvidenceSettings(enabled=False),
    )

    assert resolution is None
    assert BUNDLE_KEY not in payload


# -------------------------------------------------------------------------- report


def test_report_states_the_settings_the_table_and_the_provenance(table):
    resolution = resolve_case_evidence(
        {"normalized_text": "Amoxicillin and metoprolol given."},
        table=table,
        as_of=AS_OF,
        network_enabled=False,
    )

    report = render_hepatotoxicity_report(resolution)

    assert report.startswith("# Hepatotoxicity Evidence Report (RUCAM Item 6)")
    for heading in ("## Resolution Settings", "## Item 6 Evidence By Drug",
                    "## Provenance", "## Notes"):
        assert heading in report
    assert "| amoxicillin | case_bundle_text | DILIrank v2 | +2 |" in report
    assert "networked tiers: disabled" in report
    assert "LT3" in report  # the DILIrank row that decided metoprolol


def test_report_handles_a_case_with_nothing_resolved(table):
    resolution = resolve_case_evidence(
        {"normalized_text": "nothing here"}, table=table, as_of=AS_OF,
        network_enabled=False,
    )

    assert "No drug was resolved for this case." in render_hepatotoxicity_report(resolution)


# ----------------------------------------------------------------------- transport


def test_urls_are_built_in_a_stable_order():
    assert build_url("https://example.test/x", {"b": 2, "a": 1}) == (
        "https://example.test/x?a=1&b=2"
    )


# ------------------------------------------------- name accuracy across sources


def test_dailymed_retries_with_the_us_spelling():
    """DailyMed holds no "sulphate" label, so the variant has to be tried."""

    def fetch_json(url):
        if "sulfate" in url:
            return {"data": [{"setid": "aaa"}], "metadata": {"total_elements": 9}}
        return {"data": [], "metadata": {"total_elements": 0}}

    finding = dailymed.resolve_dailymed(
        "Morphine sulphate",
        fetch_json=fetch_json,
        fetch_text=lambda _: spl(("34066-1", "Hepatic failure reported.")),
    )

    assert finding.label_found is True
    assert finding.search_name == "morphine sulfate"
    assert finding.searched_names[0] == "morphine sulphate"
    assert finding.score == 2


def test_dailymed_stops_at_the_first_name_that_matches():
    calls: list[str] = []

    dailymed.resolve_dailymed(
        "Amiodarone hydrochloride",
        fetch_json=lambda url: (
            calls.append(url)
            or {"data": [{"setid": "aaa"}], "metadata": {"total_elements": 1}}
        ),
        fetch_text=lambda _: spl(("34066-1", "Hepatotoxicity.")),
    )

    assert len(calls) == 1


def test_dailymed_records_every_name_it_tried_when_nothing_matched():
    finding = dailymed.resolve_dailymed(
        "Ciclosporin",
        fetch_json=lambda _: {"data": [], "metadata": {"total_elements": 0}},
        fetch_text=unreachable_fetcher,
    )

    assert finding.label_found is False
    assert finding.search_name is None
    assert "cyclosporine" in finding.searched_names


def test_pubmed_query_ors_the_published_name_variants():
    """A paper using the other spelling still counts as a published case."""

    query = literature.build_pubmed_query("frusemide", as_of=AS_OF)

    assert '"frusemide"[Title/Abstract]' in query
    assert '"furosemide"[Title/Abstract]' in query
    assert query.startswith("(")


def test_pubmed_query_stays_simple_for_a_name_with_no_variants():
    query = literature.build_pubmed_query("Tolvaptan", as_of=AS_OF)

    assert query.startswith('"Tolvaptan"[Title/Abstract] AND')


#: How case reports name a drug, against the DILIrank row it must reach.
#: These are the forms that made recall 38% before name variants were added.
PUBLISHED_NAME_FORMS = (
    ("Amoxicillin", "Amoxicillin"),
    ("amiodarone", "Amiodarone hydrochloride"),
    ("Amiodarone HCl", "Amiodarone hydrochloride"),
    ("morphine sulphate", "Morphine sulfate"),
    ("sodium valproate", "Valproic acid"),
    ("valproate", "Valproic acid"),
    ("paracetamol", "Acetaminophen"),
    ("sulphasalazine", "Sulfasalazine"),
    ("amoxycillin", "Amoxicillin"),
    ("oestradiol", "Estradiol valerate"),
    ("cefalexin", "Cephalexin"),
    ("ciclosporin", "Cyclosporine"),
    ("rifampicin", "Rifampin"),
    ("frusemide", "Furosemide"),
    ("glibenclamide", "Glyburide"),
    ("lignocaine", "Lidocaine"),
    ("salbutamol", "Albuterol"),
    ("phenobarbitone", "Phenobarbital sodium"),
    ("tetracycline", "Tetracycline phosphate complex"),
    ("indometacin", "Indomethacin"),
    ("mesalazine", "Mesalamine"),
    ("methotrexate", "Methotrexate sodium"),
)

#: Forms that deliberately do not resolve offline. Brand names are not in
#: DILIrank at all, and sulfamethoxazole has no row in version 2. Both reach the
#: DailyMed and PubMed tiers when supplied with --drug.
KNOWN_UNRESOLVED_FORMS = ("Augmentin", "Tylenol", "sulphamethoxazole")


@pytest.mark.parametrize(("published", "expected"), PUBLISHED_NAME_FORMS)
def test_published_name_forms_resolve_against_the_pinned_workbook(published, expected):
    assert expected in [
        record.compound_name for record in load_dilirank_table().lookup(published)
    ]


@pytest.mark.parametrize("published", KNOWN_UNRESOLVED_FORMS)
def test_the_documented_offline_gaps_are_still_the_only_gaps(published):
    """Fails if a gap closes, so the documented accuracy stays true."""

    assert load_dilirank_table().lookup(published) == ()


def test_variant_keys_never_merge_two_different_drugs():
    """A variant that collides with another drug would silently move a score."""

    table = load_dilirank_table()

    for key, records in table.index.items():
        moieties = {
            record.compound_name.split()[0].lower().rstrip(",") for record in records
        }
        assert len(moieties) == 1, f"key {key!r} merges {sorted(moieties)}"


def test_published_alternatives_are_found_when_scanning_case_text():
    resolution = resolve_case_evidence(
        {"normalized_text": "She took paracetamol and ciclosporin, then frusemide."},
        as_of=AS_OF,
        network_enabled=False,
        fetch_json=unreachable_fetcher,
        fetch_text=unreachable_fetcher,
    )

    assert {finding.matched_names[0] for finding in resolution.findings} == {
        "Acetaminophen",
        "Cyclosporine",
        "Furosemide",
    }


# ------------------------------------------------------------------- packaging


def test_the_workbook_ships_with_the_docker_image():
    """Without it the DILIrank tier resolves nothing and item 6 silently hits 0."""

    repo_root = default_dilirank_path().parents[1]
    dockerfile = (repo_root / "Dockerfile").read_text(encoding="utf-8")
    dockerignore = (repo_root / ".dockerignore").read_text(encoding="utf-8")

    assert "COPY dilirank_v2 /app/dilirank_v2" in dockerfile
    assert "dilirank_v2" not in dockerignore.split()


def test_the_default_workbook_path_points_at_a_real_file():
    assert default_dilirank_path().is_file()
