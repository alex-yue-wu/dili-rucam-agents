# Native-SDK Analyst Tier Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the CrewAI analyst execution path with direct vendor-SDK calls, a declared roster of any size from one upward, and a validated JSON output contract whose Sections A/B/C are rendered in Python.

**Architecture:** A provider layer (one coroutine per vendor SDK) sits under a runner that separates four retry budgets. The model returns one validated JSON object carrying no total and no category; Python computes both and renders the markdown report. The roster moves from a fixed seven-element tuple plus `use_analyst_*` booleans to a JSON file, defaulting to one analyst.

**Tech Stack:** Python 3.12+, pydantic v2, `openai>=2.54.0`, `anthropic>=1.11.0`, `google-genai>=2.28.0`, pytest, openpyxl. No CrewAI, no LiteLLM.

**Spec:** [docs/superpowers/specs/2026-11-09-native-sdk-analyst-tier-design.md](../specs/2026-11-09-native-sdk-analyst-tier-design.md)

## Global Constraints

- **No live API call anywhere in the build.** Every test drives a `FakeProvider`. The user executes all paid runs.
- `rules_version` stays `RUCAM_DananBenichou1993_modified_sheet_v3`. The diff on the region between `<!-- BEGIN SHARED RUCAM CORE v3 -->` and `<!-- END SHARED RUCAM CORE v3 -->` must be empty in both prompt files.
- Temperature 0 where the model accepts one; omitted where the model rejects one.
- Default `max_output_tokens` 32000, truncation ceiling 64000.
- `TERMINAL_STATUS_CODES = frozenset({400, 401, 403, 404, 422})`.
- Validation retries default 2 (`max_retries`); transient retries 3; truncation escalations 1; mode ladder `json_schema` -> `json_object` -> `text`.
- `strict=False` on every provider's structured-output request. Optional fields are how the schema records what a source did not report.
- `extra="forbid"` on every new pydantic model.
- Report filenames are unchanged: `<key with underscores as hyphens>_report.md`, e.g. `analyst-alpha_report.md`.
- Section C's rendered JSON keeps today's field names and shape, so `extract_section_c_json`, the workbook writers and `validate_analyst_report` keep working.
- `CHECKPOINT_SCHEMA_VERSION` becomes 4.

## Review Focus

Five conditions the spec implies that no task's happy path exercises. Each has a test assigned to the task that owns the code.

1. **Two roster entries sharing a `key`** — both map to one report filename, so one analyst silently overwrites the other and the workbook loses a column. Must be rejected at load. (Task 1)
2. **A roster naming a provider whose API key is unset** — must fail before ingestion runs, not after a PDF has been parsed and an evidence cascade resolved. (Task 2)
3. **A missing or malformed roster file** — must raise an error naming the path, not a bare `JSONDecodeError` or `KeyError`. (Task 1)
4. **A `max_output_tokens` too small for the schema** — one doubling, then a `truncation` failure. It must never double past the 64000 ceiling and must never loop. (Task 3)
5. **A resumed run whose roster changed** — removing an analyst must leave every other analyst's completed checkpoint valid, and a completed report whose `<key>_assessment.json` sidecar is missing must not count as complete. (Task 11)

---

### Task 1: Roster model and config files

**Files:**
- Create: `src/dili_rucam_agents/roster.py`
- Create: `config/analysts.json`, `config/analysts.study.json`, `config/analysts.smoke.json`
- Test: `tests/test_roster.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Provider` (StrEnum: `openai`, `anthropic`, `google`, `openrouter`, `deepseek`); `AnalystConfig(key: str, label: str, provider: Provider, model: str, max_output_tokens: int = 32000)` frozen, `extra="forbid"`; `GroundTruthConfig(provider, model, max_output_tokens=8000)`; `AnalystRoster(analysts: list[AnalystConfig] = Field(min_length=1), ground_truth: GroundTruthConfig)`; `DEFAULT_ROSTER_PATH: Path`; `load_roster(path: Path | str | None = None) -> AnalystRoster`; `RosterError(ValueError)`; `AnalystConfig.report_filename -> str`.

- [ ] **Step 1: Write the failing tests**

```python
def test_single_analyst_roster_loads():
    roster = load_roster(fixture("one.json"))
    assert len(roster.analysts) == 1
    assert roster.analysts[0].key == "analyst_alpha"

def test_report_filename_replaces_underscores_with_hyphens():
    assert AnalystConfig(key="analyst_alpha", label="Analyst Alpha",
                         provider="openai", model="gpt-5.5").report_filename == "analyst-alpha_report.md"

def test_empty_analyst_list_is_rejected():  # min_length=1
def test_duplicate_key_is_rejected():       # Review Focus 1
def test_unknown_field_is_rejected():       # extra="forbid"
def test_key_must_match_pattern():          # "Analyst-Alpha" rejected
def test_missing_file_names_the_path():     # Review Focus 3
    with pytest.raises(RosterError, match="nope.json"):
        load_roster("nope.json")
def test_malformed_json_names_the_path():   # Review Focus 3
def test_shipped_default_roster_has_exactly_one_analyst():
    assert len(load_roster().analysts) == 1
def test_shipped_study_roster_reproduces_todays_seven_models():
    # the seven keys and the seven model strings, asserted literally
def test_shipped_smoke_roster_covers_every_provider():
    assert {a.provider for a in load_roster(SMOKE).analysts} == set(Provider)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_roster.py -v`
Expected: FAIL with `ModuleNotFoundError: dili_rucam_agents.roster`

- [ ] **Step 3: Implement `roster.py`**

Duplicate-key and key-pattern checks go in a `model_validator(mode="after")` on `AnalystRoster`. `load_roster` wraps `OSError` and `json.JSONDecodeError` in `RosterError` with the resolved path in the message.

- [ ] **Step 4: Write the three config files**

`config/analysts.json`: one entry, `analyst_alpha` / `Analyst Alpha` / `openai` / `gpt-5.5` / 32000, with `ground_truth` on `openai` / `gpt-5.5` / 8000.
`config/analysts.study.json`: the seven keys and models from `src/dili_rucam_agents/crew/config.py:ANALYST_SPECS` — alpha `gpt-5.5` (openai), beta `gemini-3.1-pro-preview` (google), gamma `moonshotai/kimi-k2.5` (openrouter), delta `deepseek-v4-pro` (deepseek), epsilon `qwen/qwen3.5-plus-02-15` (openrouter), zeta `claude-opus-4-7` (anthropic), eta `z-ai/glm-5` (openrouter).
`config/analysts.smoke.json`: five entries, one per `Provider`, reusing models from the study roster.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_roster.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/dili_rucam_agents/roster.py config/ tests/test_roster.py
git commit -m "feat: declare the analyst roster in a config file"
```

---

### Task 2: Provider layer

**Files:**
- Create: `src/dili_rucam_agents/providers.py`
- Test: `tests/test_providers.py`

**Interfaces:**
- Consumes: `Provider` from Task 1.
- Produces: `StructuredMode` (StrEnum `json_schema`/`json_object`/`text`); `ModelSpec(provider, model, max_output_tokens=32000, temperature=0.0)` frozen; `ModelResponse(text: str, truncated: bool = False, input_tokens: int | None, output_tokens: int | None)`; `CompletionProvider` Protocol with `async def complete(spec, messages, mode, schema) -> ModelResponse`; `SdkProvider`; `takes_temperature(model: str) -> bool`; `openai_request(spec, messages, mode, schema) -> dict`; `anthropic_request(...)`; `google_request(...)`; `extract_json(text: str) -> dict[str, Any]`; `MissingApiKeyError(RuntimeError)`; `api_key_env(provider: Provider) -> str`; `require_api_keys(providers: Iterable[Provider]) -> None`; `BASE_URLS: dict[Provider, str]`; `TIMEOUT_SECONDS = 600`;
  `model_spec(config: AnalystConfig | GroundTruthConfig) -> ModelSpec`, the one
  place a roster entry becomes a call spec — `providers.py` may import from
  `roster.py`, never the reverse.

Keys come from the environment and one client per provider is reused for a run — unlike DILIgence, which builds a client per call because its keys are per request.

- [ ] **Step 1: Write the failing tests, one per provider fact**

```python
def test_temperature_boundary_after_gpt_prefix():
    assert takes_temperature("gpt-50-turbo") is True   # the \b bug being fixed
    assert takes_temperature("gpt-5.5") is False

def test_claude_models_that_accept_temperature_keep_it():
    assert takes_temperature("claude-haiku-4-5") is True
    assert takes_temperature("claude-opus-4-7") is False

def test_openrouter_id_is_judged_on_the_bare_model_name():
    assert takes_temperature("anthropic/claude-sonnet-5") is False

def test_openrouter_request_requires_parameters():
    req = openai_request(spec(Provider.openrouter), MSGS, StructuredMode.json_schema, SCHEMA)
    assert req["extra_body"]["provider"]["require_parameters"] is True

def test_json_schema_mode_is_not_strict():
    assert req["response_format"]["json_schema"]["strict"] is False

def test_text_mode_carries_no_response_format():
def test_anthropic_request_streams():        # stream=True; large max_tokens is refused otherwise
def test_anthropic_system_prompt_is_split_out_of_messages():
def test_google_request_sums_thinking_tokens():
    # candidates_token_count + thoughts_token_count
def test_google_errors_are_normalised_to_status_code():  # google carries `code`
def test_deepseek_uses_the_openai_path_with_its_own_base_url():
def test_openai_reasoning_model_uses_max_completion_tokens_not_max_tokens():
def test_openrouter_uses_max_tokens():
def test_model_spec_carries_the_rosters_provider_model_and_cap():
def test_model_spec_omits_temperature_for_a_model_that_rejects_one():
def test_extract_json_reads_a_fenced_object():
def test_require_api_keys_names_the_missing_variable(monkeypatch):  # Review Focus 2
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(MissingApiKeyError, match="ANTHROPIC_API_KEY"):
        require_api_keys([Provider.anthropic])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_providers.py -v`
Expected: FAIL with `ModuleNotFoundError: dili_rucam_agents.providers`

- [ ] **Step 3: Implement `providers.py`**

`takes_temperature` matches `r"^(gpt-[56]|o1|o3|o4|claude-(fable|mythos|opus-5|opus-4[-.][78]|sonnet-5))\b"` against `model.split("/")[-1]`. The request builders are pure functions returning kwargs dicts, so every fact above is testable without a transport. `SdkProvider.complete` dispatches on `spec.provider`: anthropic to `_complete_anthropic`, google to `_complete_google`, everything else to `_complete_openai` with the provider's `BASE_URLS` entry. SDK-level retries off (`max_retries=0`) — the runner owns retrying. Anthropic responses keep only blocks whose `type == "text"`, so thinking blocks never reach the report.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_providers.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/dili_rucam_agents/providers.py tests/test_providers.py
git commit -m "feat: call each vendor SDK directly"
```

---

### Task 3: Runner with four separated budgets

**Files:**
- Create: `src/dili_rucam_agents/runner.py`
- Create: `tests/fakes.py`
- Modify: `src/dili_rucam_agents/providers.py` (add `SUPPORTED_MODES` / `starting_mode`)
- Test: `tests/test_runner.py`, `tests/test_providers.py`

**Interfaces:**
- Consumes: `CompletionProvider`, `ModelSpec`, `ModelResponse`, `StructuredMode` from Task 2.
- Produces: `SUPPORTED_MODES: dict[Provider, tuple[StructuredMode, ...]]` and
  `starting_mode(provider: Provider) -> StructuredMode` in `providers.py`;
  `Attempt(number: int, mode: StructuredMode, outcome: str, raw_text: str | None)`; `RunResult(value, attempts, counters: RetryCounters, input_tokens, output_tokens)`; `RetryCounters(validation_retries: int, transient_retries: int, truncation_escalations: int, mode_downgrades: int, final_mode: StructuredMode)`; `RunnerError(kind: FailureKind, attempts: int, counters: RetryCounters, execution_diagnostic, validation_diagnostic)`; `async def run_contract(*, instructions: str, model_class: type[BaseModel], spec: ModelSpec, provider: CompletionProvider, max_retries: int = 2, max_output_ceiling: int = 64000, on_attempt: Callable[[Attempt], None] | None = None, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> RunResult`; `FakeProvider` in `tests/fakes.py`, queueing scripted `ModelResponse` values or exceptions.

The correction turn sends the model's own previous reply followed by the correction, so "keep every other value the same" refers to values the model can see.

- [ ] **Step 1: Write the failing tests, one per budget**

```python
def test_valid_first_reply_spends_no_retries():
    assert result.counters == RetryCounters(0, 0, 0, 0, StructuredMode.json_schema)

def test_schema_violation_spends_a_validation_retry():
def test_validation_retry_sends_back_the_previous_reply_then_the_correction():
    assert fake.last_messages[-2]["role"] == "assistant"
def test_exhausted_validation_budget_raises_validation_kind():
def test_transient_error_does_not_consume_a_validation_attempt():
    # 429 then a valid reply -> counters.validation_retries == 0, len(attempts) == 1
def test_a_fourth_consecutive_transient_error_raises_execution_kind():
    # the budget is 3 retries, so three errors still recover; the fourth fails
def test_transient_backoff_grows_and_is_jittered(recorded_sleeps):
def test_truncated_reply_doubles_max_output_tokens_once():
def test_second_truncation_raises_truncation_kind():
def test_truncation_never_exceeds_the_ceiling():          # Review Focus 4
    # spec at 40000, ceiling 64000 -> escalates to 64000, not 80000, then fails
def test_format_rejection_steps_down_the_mode_ladder():
    assert result.counters.mode_downgrades == 1
def test_recurring_format_rejection_in_text_mode_is_terminal():
def test_401_fails_immediately_with_terminal_kind():
def test_mode_downgrade_does_not_append_an_attempt():
def test_schema_is_in_the_prompt_in_every_mode():
def test_the_ladder_starts_at_the_highest_mode_the_provider_supports():
    # DeepSeek declares json_object as its highest, so a DeepSeek run starts
    # there and never spends a call discovering json_schema is unsupported
    assert starting_mode(Provider.deepseek) is StructuredMode.json_object
    assert starting_mode(Provider.openai) is StructuredMode.json_schema
def test_a_mode_never_supported_is_not_counted_as_a_downgrade():
    # counters.mode_downgrades stays 0 on a clean DeepSeek run, so the counter
    # keeps measuring real provider refusals
def test_openrouter_format_refusal_is_a_format_rejection_not_a_terminal_404():
    # OpenRouter signals "this endpoint cannot honour response_format" with a
    # 404, which is in TERMINAL_STATUS_CODES; treated as terminal it would kill
    # the run instead of stepping down the ladder
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_runner.py -v`
Expected: FAIL with `ModuleNotFoundError: dili_rucam_agents.runner`

- [ ] **Step 3: Implement `runner.py`**

The ladder starts at `starting_mode(spec.provider)`, not unconditionally at
`json_schema`: a provider that never supported a mode should not cost a call to
discover that, and should not inflate `mode_downgrades`, which is the counter the
acceptance gate reads to confirm or kill the OpenRouter routing hypothesis.

`_is_format_rejection` must recognise OpenRouter's format refusal, which arrives
as a 404 — a status code otherwise in `TERMINAL_STATUS_CODES`. Classifying it as
terminal would end the run where the ladder should step down.

One `while len(attempts) <= max_retries` loop. A transient error or a mode downgrade `continue`s without appending to `attempts`, which is why `RetryCounters` exists: a counter derived from `attempts` cannot see them. Backoff is `0.5 * 2**transient_used` capped at 4.0 seconds plus jitter, through the injected `sleep`. A status code in `TERMINAL_STATUS_CODES` that is not a format rejection raises immediately.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_runner.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/dili_rucam_agents/runner.py tests/test_runner.py tests/fakes.py
git commit -m "feat: separate transport retries from scoring attempts"
```

---

### Task 4: Assessment schema and scoring

**Files:**
- Create: `src/dili_rucam_agents/validators/rucam_assessment.py`
- Modify: `src/dili_rucam_agents/validators/rucam_json.py` (define `ITEM_LEGAL_SCORES`, tighten `RucamScores` to it, keep `RucamReport` as the Section C contract)
- Test: `tests/test_rucam_assessment.py`

**Interfaces:**
- Consumes: `expected_category` from `validators/rucam_json.py`.
- Produces: `ITEM_LEGAL_SCORES: dict[str, frozenset[int]]`; `DataStatus` (StrEnum `reported`/`inferred`/`not_reported`); `EvidenceRef(quote: str, verified: bool = False)`; `ItemScore(score: int, data_status: DataStatus, rationale: str, inference_note: str | None = None, evidence: list[EvidenceRef] = [])`; `PreFlight` with the eleven named fields; `CauseStatus` (StrEnum `ruled_out`/`not_documented`/`present`); `ItemFiveCauses` with the six Group I and two Group II fields plus `g1: int`; `RRatioBasis(alt: float | None, alp: float | None, alt_uln: float, alp_uln: float)`; `RucamAssessment` carrying `injury_pattern`, `r_ratio`, `r_ratio_basis`, `onset_day`, `onset_anchor_dose`, `pre_flight`, `item_5_causes`, the seven `ItemScore` fields, and no total and no category; `ScoredAssessment(total_score: int, category: CausalityCategory)`; `score_assessment(assessment: RucamAssessment) -> ScoredAssessment`.

`ITEM_LEGAL_SCORES` is defined in `rucam_json.py`, not in the new module:
`rucam_assessment.py` already imports `expected_category` from it, so that is the
lower layer and placing the constant there cannot cycle, while Task 6's prompt test
needs it without pulling in the assessment schema.

**Both validators enforce it, from that one constant.** `RucamScores` keeps its
field names and its `other_causes_excluded` / `alternative_causes_excluded`
`AliasChoices`, but its `Field(ge=.., le=..)` envelopes are replaced by exact set
membership. Verified on this checkout: none of the nine tests in
`tests/test_rucam_json_validator.py` relies on a value outside the legal sets, so
this tightening breaks nothing. Without it a rendered Section C carrying
`concomitant_drugs: +2` would still validate, which is the hole the spec names.

`ITEM_LEGAL_SCORES` holds the exact D8 sets: `time_to_onset {0,1,2}`, `course {-2,0,1,2,3}`, `risk_factors {0,1,2}`, `concomitant_drugs {-3,-2,-1,0}`, `other_causes_excluded {-3,-2,0,1,2}`, `known_hepatotoxicity {0,1,2}`, `rechallenge {-2,0,1,3}`.

- [ ] **Step 1: Write the failing tests**

```python
def test_each_item_accepts_only_its_legal_set():
    # parametrised over ITEM_LEGAL_SCORES: every legal value passes,
    # and concomitant_drugs=+2 / rechallenge=+2 are rejected though both are
    # inside today's loose (-3, 3) envelope
def test_assessment_carries_no_total_and_no_category():
    assert "total_score" not in RucamAssessment.model_fields
    assert "category" not in RucamAssessment.model_fields
def test_score_assessment_sums_the_seven_items_and_derives_the_category():
def test_g1_mismatch_is_rejected():
    # six Group I + two Group II flags with three ruled_out but g1=4 -> ValidationError
def test_g1_counts_only_ruled_out():
def test_evidence_verified_is_forced_false_even_when_the_model_sends_true():
def test_inference_note_is_required_when_data_status_is_inferred():
def test_unknown_field_is_rejected():
def test_pre_flight_requires_all_eleven_answers():
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_rucam_assessment.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement the schema**

`g1` is recomputed from the eight cause flags in a `model_validator(mode="after")` and a mismatch raises — the arithmetic stops being the model's job, as `total_score` already has. `EvidenceRef.verified` is forced to `False` in a field validator: a model may supply a quote, only code may certify it.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_rucam_assessment.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/dili_rucam_agents/validators/ tests/test_rucam_assessment.py
git commit -m "feat: validate the assessment and compute the total in code"
```

---

### Task 5: Report renderer

**Files:**
- Create: `src/dili_rucam_agents/rendering.py`
- Test: `tests/test_rendering.py`

**Interfaces:**
- Consumes: `RucamAssessment`, `ScoredAssessment`, `score_assessment` from Task 4; `validate_analyst_report` from `validators/analyst_report.py`.
- Produces: `render_report(*, assessment: RucamAssessment, scored: ScoredAssessment, analyst_label: str, model_name: str, rules_version: str) -> str`; `RULES_VERSION = "RUCAM_DananBenichou1993_modified_sheet_v3"`.

- [ ] **Step 1: Write the failing tests**

```python
def test_rendered_report_passes_the_existing_report_validator():
    validate_analyst_report(render_report(...))   # the compatibility guarantee
def test_section_c_json_matches_todays_field_names():
    payload = extract_section_c_json(render_report(...))
    assert set(payload) == set(RucamReport.model_fields) | {"rules_version", "notes"}
def test_section_c_total_and_category_come_from_score_assessment():
def test_headings_are_level_two_in_a_b_c_order():
def test_section_b_table_lists_all_seven_items_with_their_scores():
def test_section_a_prints_the_g1_checklist_and_the_g1_line():
    assert "g1 = 3" in report
def test_section_a_prints_the_eleven_line_pre_flight_check():
def test_section_a_prints_onset_day_and_the_r_ratio_basis():
def test_rules_version_is_rendered_not_taken_from_the_model():
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_rendering.py -v`
Expected: FAIL with `ModuleNotFoundError: dili_rucam_agents.rendering`

- [ ] **Step 3: Implement `render_report`**

Sections A, B and C as `## SECTION A — ...` level-2 headings in order, Section C holding exactly one fenced `json` block. `rules_version` comes from the module constant, never from the model.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_rendering.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/dili_rucam_agents/rendering.py tests/test_rendering.py
git commit -m "feat: render the analyst report in Python"
```

---

### Task 6: Prompt output-contract regions

**Files:**
- Modify: `src/dili_rucam_agents/prompts/rucam_analysis_production_strict.md` (OUTPUT FORMAT and REQUIRED ANALYSIS WORKFLOW regions only)
- Modify: `src/dili_rucam_agents/prompts/rucam_analysis_production_inferring.md` (same regions)
- Modify: `tests/test_prompt_rules_sync.py`
- Test: `tests/test_prompt_rules_sync.py`

**Interfaces:**
- Consumes: `ITEM_LEGAL_SCORES` from Task 4; `RucamAssessment` from Task 4.
- Produces: prompts asking for one JSON object matching `RucamAssessment`, with no `total_score` and no `category`.

Everything between `<!-- BEGIN SHARED RUCAM CORE v3 -->` and `<!-- END SHARED RUCAM CORE v3 -->` is untouched. D8 stays literal text — a generated table would make the prompt's bytes a function of code, and the contract `sha256` feeds every checkpoint fingerprint.

- [ ] **Step 1: Rewrite the three output-contract tests and add the D8 sync test**

```python
def test_section_c_template_matches_the_assessment_schema():
    # replaces the total_score/category assertions
def test_pre_flight_check_covers_the_item_6_source():
    # rewritten against the structured pre-flight field names
def test_every_scoring_item_declares_its_allowed_values():
    # rewritten: the literal D8 table agrees with ITEM_LEGAL_SCORES
def test_d8_table_matches_the_code_constant():
    # parsed per item out of both prompt files, compared to ITEM_LEGAL_SCORES
def test_prompts_no_longer_ask_for_a_total_or_a_category():
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_prompt_rules_sync.py -v`
Expected: FAIL on the four rewritten/new tests

- [ ] **Step 3: Edit the two prompts' output regions**

Also fix a pre-existing inconsistency Task 4's review surfaced: the workflow
section says "answer all eleven lines" (line 748) while the output-format
section says "The ten-line pre-flight check" (line 784). Both are outside the
frozen core, which ends at line 613, so both are in scope here. Eleven is
correct — the schema carries eleven named pre-flight fields.

Leave the pre-flight's own wording otherwise alone, including the lines telling
the model to show the addition and to check the category against the total.
Code now recomputes both and does not read the model's answers, but those lines
are forced-reasoning scaffold whose answers the schema captures as pre-flight
fields, and rewording them is a prompt change that could move scores.

- [ ] **Step 4: Run the whole sync file to verify the frozen region is untouched**

Run: `uv run pytest tests/test_prompt_rules_sync.py -v && git diff -U0 -- src/dili_rucam_agents/prompts/ | grep -c 'SHARED RUCAM CORE'`
Expected: PASS, and the shared-core byte-identity, item 6 cascade and day-numbering tests pass unchanged

- [ ] **Step 5: Commit**

```bash
git add src/dili_rucam_agents/prompts/ tests/test_prompt_rules_sync.py
git commit -m "feat: ask the analyst for one JSON object"
```

---

### Task 7: Analyst instruction contract

**Files:**
- Create: `src/dili_rucam_agents/contracts.py`
- Test: `tests/test_contracts.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `AnalystInstructionContract` — frozen, `extra="forbid"`, fields `schema_version: int`, `analyst_label: str`, `agent_role: str`, `agent_goal: str`, `agent_backstory: str`, `instruction_template: str`, `retry_instruction_template: str`, `expected_output: str`; `.sha256` over every field; `.render(**values) -> str` raising on an unfilled placeholder; `build_analyst_instruction_contract(*, analyst_label: str, prompt_text: str, model_name: str, bundle_input_name: str) -> AnalystInstructionContract`.

The per-analyst persona is preserved. DILIgence has none; dropping ours would make every analyst's prompt identical, which is a score-relevant change inside a transport migration.

- [ ] **Step 1: Write the failing tests**

```python
def test_contract_keeps_the_per_analyst_persona():
    assert "Analyst Alpha" in contract.agent_backstory
    assert "board-certified hepatologist" in contract.agent_backstory
def test_two_analysts_have_different_sha256():
def test_sha256_is_stable_across_builds_with_the_same_inputs():
def test_render_raises_on_an_unfilled_placeholder():
def test_render_inserts_the_case_bundle_under_its_declared_name():
def test_contract_is_frozen_and_forbids_extra_fields():
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_contracts.py -v`
Expected: FAIL with `ModuleNotFoundError: dili_rucam_agents.contracts`

- [ ] **Step 3: Implement `contracts.py`**

Port the persona strings and the task-description opening from `src/dili_rucam_agents/crew/tasks.py:59-124` verbatim, minus the ReAct-specific expected-output text. `render` uses a `dict` subclass whose `__missing__` raises, as DILIgence's `_Strict` does.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_contracts.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/dili_rucam_agents/contracts.py tests/test_contracts.py
git commit -m "feat: build one instruction contract per analyst"
```

---

### Task 8: Diagnostics and checkpoint schema 4

**Files:**
- Modify: `src/dili_rucam_agents/diagnostics.py:10-30`
- Modify: `src/dili_rucam_agents/checkpoints.py:28` and `record_completed` / `record_failure` / `load_compatible_reports`
- Test: `tests/test_checkpoints.py`, `tests/test_diagnostics.py`

**Interfaces:**
- Consumes: `RetryCounters` from Task 3.
- Produces: `FailureKind = Literal["validation", "execution", "truncation", "terminal"]`; `CHECKPOINT_SCHEMA_VERSION = 4`; `AnalystIdentity` gains `assessment_filename: str`; `record_completed(identity, *, attempt, report_text, assessment_json, counters)`; manifest entries carrying `validation_retries`, `transient_retries`, `truncation_escalations`, `mode_downgrades`, `final_mode`.

- [ ] **Step 1: Write the failing tests**

```python
def test_failure_kind_accepts_truncation_and_terminal():
def test_validation_field_paths_cover_the_assessment_schema():
def test_runner_validation_diagnostic_carries_assessment_field_paths():
    # until the allowlist admits them the runner can only emit
    # ("report", "invalid_value"); widening the frozenset alone does not make
    # the diagnostic precise, so the pydantic-error-to-path mapping lands here
def test_schema_version_is_four():
def test_a_manifest_at_version_three_is_discarded():
def test_completed_entry_records_the_four_counters():
def test_completed_requires_both_the_report_and_the_sidecar():   # Review Focus 5
    # delete <key>_assessment.json -> all_completed() is False and the
    # analyst is re-scored
def test_removing_an_analyst_leaves_the_others_checkpoints_valid():  # Review Focus 5
def test_fingerprint_ignores_the_roster():
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_checkpoints.py tests/test_diagnostics.py -v`
Expected: FAIL on the new assertions

- [ ] **Step 3: Implement the changes**

`_VALIDATION_FIELD_PATHS` is a closed frozenset that raises on an unknown path, so every new assessment path is added to it. Widening it is necessary but not sufficient: `runner.run_contract` currently emits a generic `("report", "invalid_value")` because no finer path was admissible, so this task also maps pydantic error locations to those paths. Without it the four retry counters record *that* a validation retry happened but never which field caused it, which is most of their diagnostic value. `enabled_analysts` stays in the manifest and out of the fingerprint: adding or removing an analyst must not invalidate the others.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_checkpoints.py tests/test_diagnostics.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/dili_rucam_agents/diagnostics.py src/dili_rucam_agents/checkpoints.py tests/
git commit -m "feat: record every retry budget in the checkpoint manifest"
```

---

### Task 9: Analyst execution

**Files:**
- Create: `src/dili_rucam_agents/analysts.py`
- Test: `tests/test_analysts.py`

**Interfaces:**
- Consumes: `AnalystRoster`, `AnalystConfig` (Task 1); `CompletionProvider`,
  `model_spec`, `require_api_keys` (Task 2); `run_contract`, `RetryCounters`,
  `RunnerError` (Task 3); `RucamAssessment`, `score_assessment` (Task 4);
  `render_report` (Task 5); `build_analyst_instruction_contract` (Task 7).
- Produces: `AnalystAttemptEvent(analyst_key, attempt, max_attempts, status, report_text, counters, execution_diagnostic, validation_diagnostic)`; `AnalystRunOutcome(reports: dict[str, str], assessments: dict[str, dict], counters: dict[str, RetryCounters])`; `run_analysts(*, case_bundle_json: str, roster: AnalystRoster, prompt_text: str, bundle_input_name: str, provider: CompletionProvider, max_retries: int = 2, completed_reports: Mapping[str, str] | None = None, on_report: Callable[[str, str, dict], None] | None = None, on_attempt: Callable[[AnalystAttemptEvent], None] | None = None) -> AnalystRunOutcome`; `AnalystExecutionError(RuntimeError)`.

Analysts run sequentially, one `asyncio.run` per `run_analysts` call. The layer is `gather`-ready; fan-out stays off per the spec's non-goals.

- [ ] **Step 1: Write the failing tests**

```python
def test_one_analyst_roster_produces_a_report_and_a_sidecar():
def test_seven_analyst_roster_produces_seven_of_each():
def test_a_completed_report_passed_in_is_not_re_scored():
def test_a_failing_analyst_raises_with_its_failure_kind_and_counters():
def test_each_analyst_gets_its_own_contract_sha256():
def test_no_analyst_sees_another_analysts_output():
def test_api_keys_are_checked_before_any_call():   # Review Focus 2
def test_rejected_attempts_persist_as_invalid_artifacts():
    # attempts/<key>.1.invalid.json when a JSON object parsed, .invalid.txt otherwise
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_analysts.py -v`
Expected: FAIL with `ModuleNotFoundError: dili_rucam_agents.analysts`

- [ ] **Step 3: Implement `run_analysts`**

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_analysts.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/dili_rucam_agents/analysts.py tests/test_analysts.py
git commit -m "feat: run analysts without an agent executor"
```

---

### Task 10: Ground-truth score finder

**Files:**
- Modify: `src/dili_rucam_agents/ground_truth.py`
- Test: `tests/test_ground_truth.py`

**Interfaces:**
- Consumes: Tasks 1-3, 7.
- Produces: `run_ground_truth_score_finder(*, raw_case_bundle_json: str, config: GroundTruthConfig, provider: CompletionProvider) -> str`, replacing `crew/crew.py:_run_ground_truth_score_finder`. `extract_ground_truth_rucam_score` and `extract_ground_truth_rucam_category` keep their signatures, so the `masked_rucam_score` / `masked_rucam_category` workbook columns and the `ground_truth_rucam` aggregate row are unchanged.

- [ ] **Step 1: Write the failing tests**

```python
def test_finder_calls_the_configured_ground_truth_model():
def test_finder_output_still_yields_a_score_and_a_category():
def test_finder_never_appears_as_an_analyst_column():
def test_missing_ground_truth_key_is_reported_before_the_call():
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_ground_truth.py -v`
Expected: FAIL

- [ ] **Step 3: Implement the migration**

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_ground_truth.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/dili_rucam_agents/ground_truth.py tests/test_ground_truth.py
git commit -m "feat: find the published score without CrewAI"
```

---

### Task 11: Wire the pipeline to the roster and delete CrewAI

**Files:**
- Modify: `src/dili_rucam_agents/pipeline.py` (`run_end_to_end`, `_build_checkpoint_context`, `is_end_to_end_complete`, `_main`, `_REPORT_FILENAME_MAP`)
- Modify: `src/dili_rucam_agents/batch.py` (`run_batch_folder`, `_run_pdf_analysis`, `_get_resolved_analyst_configs`, `_main` argparse)
- Modify: `src/dili_rucam_agents/reproducibility.py` (`run_reproducibility_folder`)
- Modify: `src/dili_rucam_agents/masking.py:8` and `src/dili_rucam_agents/ingestion/build_bundle.py:7` (drop the `crewai.tools.BaseTool` subclasses `ScoreMaskingTool` and `CaseBundleExtractionTool`; the pipeline calls the functions directly)
- Modify: `scripts/run_batch.py`, `pyproject.toml`, `Dockerfile`
- Delete: `src/dili_rucam_agents/crew/` (all four modules), `src/dili_rucam_agents/litellm_runtime.py`, `tests/test_crew_topology.py`, `tests/test_agents.py`
- Modify: `tests/test_batch.py` (35 `use_analyst_*` sites), `tests/test_reproducibility.py` (5 sites), `tests/test_masking.py`, `tests/test_ingestion.py`
- Test: `tests/test_batch.py`, `tests/test_reproducibility.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `run_end_to_end(pdf_path, prompt_path=None, output_dir=None, *, roster_path: str | None = None, enable_score_masking=False, strict_scoring=False, max_restarts=2, resume=True, hepatotoxicity_settings=None) -> str`; the same `roster_path` parameter on `run_batch_folder` and `run_reproducibility_folder`; `--analysts <path>` on both CLIs. The four `use_analyst_*` parameters and the `--analyst-delta/-epsilon/-zeta/-eta` flags are gone.

`_REPORT_FILENAME_MAP` keeps its three non-analyst entries and derives analyst filenames from `AnalystConfig.report_filename`.

- [ ] **Step 1: Write the failing tests**

```python
def test_default_run_scores_one_analyst():
    # no --analysts -> one score column, one aggregate row
def test_analysts_flag_selects_the_study_roster():
def test_removed_analyst_flags_are_rejected_by_the_parser():
def test_workbook_columns_follow_the_roster_order():
def test_aggregate_sheet_has_one_row_per_roster_analyst_plus_ground_truth():
def test_single_analyst_aggregate_row_reports_no_sample_standard_deviation():
def test_reproducibility_resume_skips_completed_repeats():
def test_missing_api_key_fails_before_the_pdf_is_ingested(monkeypatch):
    # Review Focus 2: the cascade and the ingestion stack are expensive and
    # the failure is knowable up front
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_batch.py tests/test_reproducibility.py -v`
Expected: FAIL

- [ ] **Step 3: Thread `roster_path` through and delete the CrewAI surface**

Remove `crewai[anthropic,google-genai,tools]>=1.8.1` and `litellm>=1.52.0` from `pyproject.toml`; add `anthropic>=1.11.0`, `openai>=2.54.0`, `google-genai>=2.28.0`.

Two things can only be done here, once the SDK floors are raised, and both were
deferred to this task by Task 2's review:

1. Delete `providers._anthropic_schema` in favour of `anthropic.transform_schema`,
   which does not exist on `anthropic 0.71.1` but does on `>=1.11.0`. Add a test
   asserting the two agree on a representative schema before removing the local
   one. The local transform was written only because the symbol was unavailable.
   Two known gaps in it are expected to disappear with the swap, and the
   agreement test should cover both: sub-schema keywords outside its table
   (`contains`, `propertyNames`, `dependentSchemas`, `unevaluatedItems`,
   `unevaluatedProperties`) are neither recursed nor closed; and an
   `additionalProperties` *sub-schema* is overwritten with `False` on any
   `type: object` node, which discards the value schema pydantic emits for a
   `dict[str, X]` field.
2. Re-verify every `google_request` kwarg against `google-genai>=2.28.0`.
   Task 2's facts were checked against the installed `1.49.0`, where
   `HttpOptions.timeout` is in milliseconds and `HttpRetryOptions(attempts=...)`
   exists; both may differ across the major bump, and neither is exercised by a
   test because neither runs without a live call.

Add `COPY config /app/config` to the `Dockerfile` beside the existing `COPY dilirank_v2 /app/dilirank_v2`. `DEFAULT_ROSTER_PATH` resolves relative to the source tree, exactly as `default_dilirank_path()` does, so without this a container run with no `--analysts` cannot find the default roster. Commit 70c572c is the same bug for the DILIrank workbook.

- [ ] **Step 3b: Remove the transitional xfail markers**

Task 8 marked 16 tests in `tests/test_batch.py` `xfail(strict=True)` because they
drive the old CrewAI path through `pipeline.py` — legacy adoption, topology
expansion and contraction, the v2 manifest replacement, and
`run_end_to_end`'s checkpoint behaviour. Once this task rewrites that path they
will XPASS, and a strict xfail turns XPASS into a failure, so the suite stays
red until the markers come off. Rewrite each test against the roster-driven
path and delete its marker; do not delete a test to make the marker go away.

Two call sites in `pipeline.py` are not merely untested but non-functional until
this task rewrites them, and Task 8's review named both: line 88 passes dict
configs into `build_analyst_identities`, which now reads `config.key` and raises
`AttributeError`, and line 138 calls `record_completed(identity, attempt=...,
report_text=...)`, which now requires `assessment_json` and `counters` and raises
`TypeError`.

One of the xfailed tests needs a value changed, not just a marker removed:
`test_schema_v2_manifest_cannot_skip_execution_and_is_replaced_cleanly` still
asserts `current_manifest["schema_version"] == 3`. That must become 4, or the
test fails for a reason unrelated to the rewrite.

Retire the store's legacy attempt-artifact writer. `AnalystCheckpointStore.record_failure`
writes `attempts/<uuid>.invalid.md` (`checkpoints.py:239`), the markdown-era scheme, while
`run_analysts` writes `attempts/<key>.<n>.invalid.json` when a JSON object parsed and
`.invalid.txt` when nothing did — which is what the spec mandates (spec lines 389-390).
Leaving both would write two artifacts per failure under different names. Keep the
spec's scheme, drop the `.invalid.md` write, and wire `on_report` to
`store.record_completed` so the report and its sidecar are persisted by the store that
validates them.

Three more call-site facts Task 9's review established, each verified against the
code rather than inferred:

- `pipeline.py:132` reads `event.error`, which the new `AnalystAttemptEvent` does
  not have (CrewAI's did, `crew.py:68-72`). Substantively harmless —
  `record_failure` recomputes the message from the diagnostic
  (`checkpoints.py:207-226`) — but the call site must change or it raises.
- `require_api_keys` in `run_analysts` covers `roster.analysts` only, not
  `roster.ground_truth.provider`. With masking enabled the pipeline also calls a
  ground-truth model, so the "every provider's key before any ingestion"
  guarantee is not met at the pipeline level until the ground-truth provider is
  included in the pre-flight check. Add it where the pipeline composes the run.
- `validate_max_restarts` still lives in `crew/crew.py:179`, which this task
  deletes. Relocate it to a neutral module and apply it to `run_analysts`'
  `max_retries`: today a programmatic `max_retries=-1` produces
  `RunnerError(kind="validation", attempts=0, validation_diagnostic=None)` and an
  `AnalystExecutionError` carrying a null diagnostic. The CLI is safe because
  argparse constrains `--analyst-restarts` to `range(0, 3)`.

Task 10's review added two more:

- Delete the duplicate `build_ground_truth_score_finder_prompt`. It now exists in
  both `crew/tasks.py:205` and `ground_truth.py:54`; removing the `crew/` copy is
  part of deleting the package, and leaving two copies would let the ground-truth
  prompt drift between them.
- `ground_truth.run_ground_truth_score_finder` calls `asyncio.run` internally
  (`ground_truth.py:95`), which raises `RuntimeError` if invoked from inside a
  running event loop. Fine for the current synchronous call site; if the wiring
  puts it on an async path, call the coroutine directly instead.

- [ ] **Step 4: Verify removal and run the whole suite**

Run: `grep -rn "crewai\|litellm\|kickoff\|use_analyst_\|ANALYST_.*_MODEL\|ANALYST_.*_MAX_TOKENS" src/ pyproject.toml; uv run pytest -q`
Expected: grep finds nothing (exit 1) — the roster is the single source of truth,
so no env-var path may survive; the suite passes

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: select analysts from a declared roster"
```

---

### Task 12: Documentation

**Files:**
- Modify: `AGENTS.md` (§2 architecture diagram, §6 roster and env contract, §8 engineering standards, §9 Definition Of Done)
- Modify: `README.md` (invocation examples, analyst flags, env-var table)
- Test: `tests/test_docs_sync.py`

**Interfaces:**
- Consumes: Task 1's shipped roster files.
- Produces: no code.

§9 currently makes "the 3 default analysts complete Sections A, B, and C" a condition of a valid run. With a default of one, that sentence would declare the default configuration invalid, so it is rewritten in terms of the declared roster.

- [ ] **Step 1: Write the failing test**

```python
def test_agents_md_describes_the_roster_not_the_env_contract():
    text = Path("AGENTS.md").read_text()
    assert "ANALYST_ALPHA_MODEL" not in text
    assert "use_analyst_" not in text
    assert "3 default analysts" not in text
    assert "config/analysts.json" in text
def test_readme_shows_the_analysts_flag_and_no_removed_flags():
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_docs_sync.py -v`
Expected: FAIL

- [ ] **Step 3: Update `AGENTS.md` and `README.md`**

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add AGENTS.md README.md tests/test_docs_sync.py
git commit -m "docs: describe the declared roster"
```

---

## Hand-Off

The build stops here. Criteria 8-10 of the spec are live runs the user executes:

```bash
uv run python scripts/run_batch.py --analysts config/analysts.smoke.json <one-pdf-dir> <out>
```

```bash
uv run python scripts/run_batch.py --analysts config/analysts.study.json --reproducibility --repeats 5 <study-pdf-dir> <out>
```

Read `validation_retries`, `transient_retries`, `truncation_escalations` and `mode_downgrades` out of each `analyst_checkpoints.json`. The gate is fewer than 5 validation retries across 70 analyst runs, none above 1. A non-zero `truncation_escalations` on the smoke run means the 32000 default is too small for the enlarged schema and should be raised before paying for the gate.
