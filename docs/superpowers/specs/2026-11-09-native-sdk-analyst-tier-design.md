# Native-SDK Analyst Tier Design

**Date:** 2026-11-09
**Status:** Decisions resolved; ready to plan

## Purpose

Replace the CrewAI analyst execution path with a direct, native-SDK call layer, and
replace the fixed seven-analyst roster with a declared roster of any size from one
upward.

The change is motivated by measurement, not taste. Across the most recent
reproducibility study (70 analyst runs over two PDFs x five repeats), 17 runs needed
at least one retry and 31 retries were spent. Twenty-six of those 31 retries were
output-format failures rather than reasoning failures, and inspection of the rejected
attempts shows most were artifacts of CrewAI's ReAct executor rather than model
failures: six rejected attempts are the literal seven-byte string `Thought`, and
thirteen more are sub-kilobyte summaries referring to a report "provided above" --
that is, the model wrote the report in an intermediate step and the executor returned
a summary of it, discarding the work.

This matters for scores, not only for cost. Every rejection re-runs the analyst with
a `retry_instruction` appended to its task description, so the retried run is scored
under a different prompt than the first. Roughly a quarter of the study's scores were
produced under a prompt perturbed by a framework artifact. It also matters for speed:
`analyst_eta` retried on 10 of 10 runs and is consequently the slowest analyst in
four of six clean repeats, which is what holds analyst fan-out to a measured ~2.5x
instead of the ~7x the topology allows.

The scoring rules do not change. `rules_version` stays
`RUCAM_DananBenichou1993_modified_sheet_v3` and the shared rules core stays
byte-identical. What changes is the transport, the output contract, and the roster.

## Goals

- Remove `crewai` and `litellm` from the codebase entirely, including the dependency
  entries.
- Call each vendor's own SDK directly: OpenAI, Anthropic, Google, OpenRouter,
  DeepSeek.
- Separate transport retries from semantic retries, so a rate-limit response can
  never consume an analyst's chances to produce a valid report.
- Accept a roster of any length from one analyst upward, declared in one place, with
  the provider stated explicitly rather than inferred from the model string.
- Ask the model for a validated JSON object and render Sections A, B and C from it in
  Python, so a well-formed assessment can no longer be rejected for prose layout.
- Stop asking the model for `total_score` and `category`; compute both from the seven
  items.
- Detect truncation from the provider's own finish reason.
- Keep the whole layer testable without live API calls.
- Preserve the resume and reproducibility-workbook behaviour, and the safe-diagnostic
  boundary.

## Non-Goals

- Changing any RUCAM scoring rule, boundary convention, worked case, or the item 6
  evidence cascade.
- Changing `rules_version`.
- Collapsing the strict and inferring prompt variants into a single call (see
  **Deferred**).
- Adding an arbiter or any cross-analyst adjudication.
- Parallelising analysts, repeats or PDFs. The layer becomes `gather`-ready; fan-out
  stays off.
- Changing ingestion, masking, or the DILIrank/DailyMed/PubMed tiers.

## Anti-Goals

Things that would make this a failure even if it technically worked:

- A change that alters a score for a reason other than removing framework noise. The
  rules core is frozen precisely so a score movement can be attributed.
- A retry that re-rolls the whole assessment when only one field was wrong.
- Provider routing that infers a provider by sniffing substrings of a model name.
  That is the mechanism being removed, not one to reimplement.
- Any path where a transient network error reduces an analyst's genuine scoring
  attempts.
- A roster format that cannot express a single-analyst run as a first-class case.
- Dropping the forced-reasoning scaffold (the pre-flight check, the Group I/II
  checklist and its `g1 = <n>` line) or the per-analyst persona. Both are
  score-relevant and both are preserved; see **Output Contract** and **Prompt
  Regions**.

## The Dependency Constraint That Shapes This Change

Verified on this checkout: `crewai 1.8.1` requires `openai~=1.83.0`,
`anthropic~=0.71.0` and `google-genai~=1.49.0`. The native-SDK layer needs the next
majors -- DILIgence pins `anthropic>=1.11.0`, `openai>=2.54.0`,
`google-genai>=2.28.0`. **These cannot co-exist**, so removing the `crewai`
dependency entry is not deferrable; it is a precondition.

Two consequences follow, and both widen the change:

1. **The ground-truth RUCAM score finder is in scope.** `_run_ground_truth_score_finder`
   (`crew/crew.py:571`) is a live `crewai.LLM` call on every masking-enabled run,
   feeding the `masked_rucam_score` / `masked_rucam_category` workbook columns and the
   `ground_truth_rucam` aggregate row. It migrates to the same provider layer with its
   own contract, model spec and API-key precheck. Its model is configured alongside
   the roster (a `ground_truth` key in the roster file), not inside the `analysts`
   list, so it never appears as an analyst column.
2. **A large share of the test suite is rewritten, not preserved.** Measured on this
   checkout: 393 tests total; `tests/test_crew_topology.py` has 46 built on fake
   `kickoff` objects and a monkeypatched `Crew`; `tests/test_agents.py` has 33, almost
   all exercising the `_build_routed_llm_kwargs` string-sniffing being deleted; and
   `use_analyst_*` appears at 35 sites in `test_batch.py` and 5 in
   `test_reproducibility.py`. Roughly 100-150 tests are rewritten or deleted. This is
   expected work, not a regression.

Also verified: on the installable `anthropic 0.71.1`, `transform_schema` does not
exist and `messages.stream` has no `output_config` -- but it *does* accept
`temperature` as a real keyword. So the Anthropic facts below apply only after the
SDK major bump, and the bump is what makes the `json_schema` tier available for Claude
models at all.

## Prior Art: What DILIgence Already Solved

The sibling repository at `/Users/alexwu/Documents/GitHub/DILIgence` has already made
this migration and runs it in production. Its `_LEGACY_PREFIX` table is documented as
"the routing prefix LiteLLM used to need ... before the SDKs took over". This design
ports its proven structure rather than inventing one.

Adopted essentially wholesale:

- **`providers.py` shape.** A `Provider` enum; a frozen `ModelSpec`; a `ModelResponse`
  carrying text, a `truncated` flag and token counts; a `CompletionProvider` Protocol
  a fake implements in tests; one `_complete_*` coroutine per vendor.
- **`runner.py` budget separation.** Four independent budgets: validation retries (the
  only ones consuming `max_retries`), transient transport retries
  (`_MAX_TRANSIENT_RETRIES = 4`, exponential backoff with jitter), truncation
  escalation (one doubling, then a hard failure), and structured-output downgrades.
  Mode downgrades and transient retries `continue` without appending to the attempt
  list.
- **`TERMINAL_STATUS_CODES = {400, 401, 403, 404, 422}`.** Retrying a bad key or model
  id cannot help.
- **The structured-output ladder.** `json_schema` -> `json_object` -> `text`, with
  `strict=False` on every provider, deliberately: strict mode requires every property
  in `required`, which these schemas violate by design because optional fields are how
  they record what a source did not report.
- **Schema in the prompt on every tier**, since the fallback tiers carry none
  natively.
- **Parse JSON on every tier**, because a provider's JSON mode is not a guarantee.
- **`InstructionContract`.** Frozen, `extra="forbid"`, `sha256` over every field, and
  a `format_map` that raises on an unfilled placeholder.
- **An N-length roster.** `analysts: list[AnalystConfig] = Field(min_length=1)`.
- **Fan-out as `asyncio.gather`** (built for, not enabled here).
- **No total and no category from the model.**
- **The retry conversation shape.** The correction turn sends back the model's own
  previous reply followed by the correction, because "'keep every other value the
  same' means nothing to a model that cannot see those values". This is a consistency
  improvement: a corrective retry perturbs one field instead of re-rolling.
- **`MissingApiKeyError`.** Every provider the run will call is key-checked before any
  ingestion or model call.

Provider-specific facts it records, all applying **after** the SDK bump:

| Fact | Consequence here |
| --- | --- |
| Anthropic's SDK refuses a large non-streaming `max_tokens` | Must stream; our ceiling is 64000 |
| Thinking blocks precede text | Filter `block.type == "text"` or reasoning lands in the report |
| Only Opus 4.7+, Opus 5, Sonnet 5, Fable, Mythos reject `temperature` | Our rule drops it from *all* Claude models, including ones that honour it |
| A `\b` is needed after `gpt-[56]` | Our `startswith` has this bug: a future `gpt-50` silently loses temperature |
| Anthropic SDK 1.x dropped the sampling keyword; the API still honours it | Pass via `extra_body` |
| OpenRouter needs `extra_body={"provider": {"require_parameters": True}}` | Else it may route to an endpoint ignoring `response_format` |
| Gemini bills thinking as output but reports it separately | `candidates_token_count + thoughts_token_count` |
| Google's SDK errors carry `code`, not `status_code` | Normalise before classifying |
| OpenRouter documents `max_tokens`; OpenAI reasoning models reject it | Cap key differs by provider |

The OpenRouter routing fact deserves emphasis. `analyst_eta` is `z-ai/glm-5` on
OpenRouter, retried 10 of 10 runs, and two rejections were truncations -- the
signature DILIgence documents for a format request routed to an endpoint that ignores
it. The ReAct executor is the established cause; this is a plausible second,
compounding one. Expect eta to need separate confirmation after the migration rather
than assuming one fix addressed both. The per-event accounting below is what makes
that confirmable.

Deliberately **not** adopted:

- **`ITEM_SCORE_RANGES` as a loose envelope.** DILIgence bounds items at `(-3, 3)` and
  `(-2, 2)`; our `RucamScores` does the same. But our D8 table states the exact legal
  set per item (`concomitant_drugs` is `{-3,-2,-1,0}`, `rechallenge` is
  `{-2,0,+1,+3}`), so a model returning `concomitant_drugs: +2` passes today. This
  design enforces exact set membership in code, which is stricter than DILIgence.
- **A client constructed per call.** DILIgence does that because its keys are
  per-request in a multi-user service. This is a single-user CLI reading keys from the
  environment, so one client per provider reused across a run is correct.
- **`Narrative` as the whole of the prose.** Its four fields carry nothing like our
  pre-flight check or `g1` checklist, and DILIgence has no Section A/B/C renderer at
  all. `render_report()` is new work with no template to copy.
- The ULN policy machinery, the Celery/FastAPI service layer, and the RECAM method.

## Prompt Regions: What Is Frozen And What Is Not

"The rule text" means the region delimited by
`<!-- BEGIN SHARED RUCAM CORE v3 -->` / `<!-- END SHARED RUCAM CORE v3 -->` (currently
lines 40-613 of each prompt): the rules, D1-D8 and the appendices. Its diff must be
empty, and `test_shared_rules_core_is_byte_identical_across_prompt_variants` keeps
protecting it.

The OUTPUT FORMAT and REQUIRED ANALYSIS WORKFLOW regions sit outside the core and are
in scope. Three tests in `tests/test_prompt_rules_sync.py` assert the old output
contract and **are edited by this change**, which supersedes any reading of "existing
tests pass unchanged":

- `test_section_c_template_matches_the_validated_schema` asserts exactly one fenced
  JSON block whose keys include `total_score` and `category`. Rewritten against the
  new schema.
- `test_pre_flight_check_covers_the_item_6_source` asserts the literal "answer all
  eleven lines". Rewritten against the structured pre-flight fields.
- `test_every_scoring_item_declares_its_allowed_values` reads D8 out of the file.
  Rewritten to assert the literal table agrees with the new code constant.

**D8 stays literal text in the prompt file.** It is not rendered at load time. A
generated table would make the prompt's bytes a function of code, and
`AnalystInstructionContract.sha256` feeds every checkpoint fingerprint -- a templated
prompt risks silently invalidating resume. A test asserting the literal table matches
the constant gives the same protection with none of that risk, following the existing
`test_prompt_score_tables_match_the_resolver_constants` precedent.

**The per-analyst persona is preserved.** Today each analyst receives
`agent_role` / `agent_goal` / `agent_backstory` ("Analyst Alpha is a board-certified
hepatologist and pharmacovigilance researcher...") plus a task description opening
"You are the {analyst_label} RUCAM Analyst". DILIgence has no per-analyst persona --
its analysts differ only by `ModelSpec`. Dropping ours would make every analyst's
prompt identical, which is a score-relevant change inside a transport migration and
is forbidden by the anti-goals. So the contract stays per-analyst, N contracts are
built per run, and each contract's `sha256` stays per-analyst exactly as today.

## Roster Configuration

`ANALYST_SPECS` -- a seven-element tuple, three always on and four gated by
`use_analyst_*` booleans threaded through `run_end_to_end`, `_run_pdf_analysis`,
`run_batch_folder` and `run_reproducibility_folder` -- is replaced by a declared
roster.

**Decision (mine):** the roster lives in a JSON file, defaulting to
`config/analysts.json`, overridable with `--analysts <path>`. Each analyst needs at
least four fields now that the provider is explicit; environment variables express a
variable-length list of records badly, and a file is diffable and committable next to
the results it produced -- which matters because the roster is an experimental
condition. API keys stay in the environment.

```json
{
  "analysts": [
    {"key": "analyst_alpha", "label": "Analyst Alpha", "provider": "openai",
     "model": "gpt-5.5", "max_output_tokens": 32000}
  ],
  "ground_truth": {"provider": "openai", "model": "gpt-5.5", "max_output_tokens": 8000}
}
```

- `analysts` has at least one entry, and one is the default, so the single-analyst
  path must work end to end: report, sidecar, workbook score column, aggregate row.
- `key` is unique, matches `[a-z][a-z0-9_]*`, and determines the report filename as
  `<key with underscores as hyphens>_report.md`, preserving
  `analyst-alpha_report.md`.
- `extra="forbid"` throughout, so a typo is an error rather than a silent default.
- The `use_analyst_*` booleans and matching CLI flags are removed.

**Three rosters ship, because building and measuring cost different amounts.** The
user executes every live call (see **Decisions Taken**), so the roster the builder
runs by hand is not the roster the gate is judged on.

| File | Entries | Purpose |
| --- | --- | --- |
| `config/analysts.json` (the default) | 1 | `analyst_alpha` on `gpt-5.5`. **This changes default behaviour**: a caller who passes no roster gets one analyst where today they get three. |
| `config/analysts.study.json` | 7 | All seven of today's analysts. This is the condition the 31-retry baseline was measured in, so it is the only roster criterion 9 can be judged on. |
| `config/analysts.smoke.json` | 5 | One analyst per provider adapter: OpenAI, Anthropic, Google, OpenRouter, DeepSeek. The cheapest run that touches every transport. |

**Decision (the user's): the default roster is one analyst.** Three consequences
follow, all accepted:

- **Omitting `--analysts` stops being equivalent to omitting every `use_analyst_*`
  flag.** Today that gives three analysts; it will give one. This is a deliberate
  behaviour change rather than a side effect of the migration, and it belongs in the
  commit message, not only in this document.
- **`AGENTS.md` changes in three places, and one of them is normative.** Its
  architecture diagram says `3 default analysts` (§2), §6 names Alpha, Beta and Gamma
  as the default analysts, and §9 makes "the 3 default analysts complete Sections A,
  B, and C" a condition of a valid run. §9 is the one that matters: a one-analyst run
  becomes a valid run by definition, so the Definition Of Done is rewritten in terms
  of the declared roster rather than a fixed count.
- **The riskiest path becomes the default path**, which is the useful part. N=1 is
  what the whole roster change hinges on and the easiest thing to leave half-wired:
  one score column, an aggregate sheet with a single scorer row, exact-mode agreement
  computed over one analyst. Making it the default means every run nobody configured
  exercises it, instead of a test being the only thing that does.

Development makes no live call at all -- the runner is driven by `FakeProvider` -- so
the default's size governs only what a manual sanity run costs, and one call is as
cheap as that gets. Every roster is reached the same way, by `--analysts <path>`; none
is privileged in code.

**No environment-variable compatibility shim.** Verified: outside `src/`, `tests/`,
`README.md` and `AGENTS.md`, nothing reads `ANALYST_*_MODEL` or
`ANALYST_*_MAX_TOKENS`; no script or runbook depends on them. The roster becomes the
single source of truth, which the fingerprint requires -- two sources would make a
fingerprint ambiguous.

**`AGENTS.md` §2, §6 and §9 and `README.md` are updated in the same change.**
`AGENTS.md` is the checked-in normative spec and currently documents the env contract,
the fixed roster, the `use_analyst_*` topology and a three-analyst Definition Of Done.
Leaving it stale would make the spec contradict the code about what counts as a valid
run.

Checkpoint interaction: the analyst fingerprint covers an analyst's own inputs, not
the roster. Adding or removing an analyst must not invalidate the others' completed
checkpoints. The manifest's `enabled_analysts` field records the roster without
participating in the fingerprint; that split is deliberate and preserved.

## Architecture

```text
case_bundle_json (unchanged, deterministic)
   |
   v
AnalystRoster (config/analysts*.json)       -- N >= 1 ModelSpecs, + ground_truth
   |
   v
InstructionContract (one per analyst)        -- persona kept; rules core verbatim
   |
   v
run_contract()                               -- four budgets, mode ladder
   |                                            validation | transient | truncation | format
   v
CompletionProvider                           -- Protocol; FakeProvider in tests
   |
   +-- _complete_openai      (OpenAI, OpenRouter, DeepSeek -- one Chat Completions path)
   +-- _complete_anthropic   (streamed; text blocks only)
   +-- _complete_google      (generate_content; thinking tokens summed)
   |
   v
RucamAssessment (pydantic, extra="forbid")   -- no total, no category
   |
   +--> score_assessment()   -- total + category computed here
   +--> render_report()      -- Sections A/B/C written in Python  --> <key>_report.md
   +--> persisted verbatim                                        --> <key>_assessment.json
   |
   v
checkpoint store (schema 4), workbook, diagnostics
```

DeepSeek reuses the OpenAI path with its own `base_url`, as `_build_routed_llm_kwargs`
already does: one enum member and one base-URL entry, not a fourth transport.

## Output Contract

The model returns one JSON object. Sections A, B and C are rendered from it. Section
C's rendered JSON keeps its current field names and shape, so
`extract_section_c_json`, the workbook writers and the existing analysis scripts keep
working.

**The schema carries the forced-reasoning scaffold.** This is forced by the frozen
core, not chosen: the shared rules core itself mandates the output. At lines 365-373
the `g1` counting procedure requires the model to "print the six Group I lines and the
two Group II lines, each marked `ruled out` or `not documented`, followed by
`g1 = <n>`", and the workflow region requires the eleven-line pre-flight check
answered with actual values. Since the core is frozen, the contract must have a place
for what the core demands. A thin schema that discarded this prose would be a prompt
change in disguise and would move scores.

So the object carries, beyond the DILIgence-derived audit fields:

- `pre_flight`: the eleven checks as named fields, each with its answered value.
- `item_5_causes`: the six Group I causes and two Group II categories, each
  `ruled_out` / `not_documented` / `present`, plus the derived `g1` integer. `g1` is
  recomputed in code from the eight flags and a mismatch is a validation error --
  the arithmetic stops being the model's job, as with `total_score`.
- Per item: `score`, `data_status`, `inference_note` (required when `inferred`),
  `rationale`, and `evidence` quotes whose `verified` flag is forced to `False` by
  the validator. A model may supply a quote; only code may certify it.
- `r_ratio_basis`: the ALT, ALP and ULN values behind the R-ratio, so the arithmetic
  is auditable.
- `onset_day` and the dose it is counted from, per the v3 day rule.

This roughly triples the schema, the renderer and the validator surface against a
thin-schema design. That cost is accepted: the alternative changes scores.

Exact per-item set membership replaces the loose envelope, enforced from one constant
that D8 is tested against.

## Persistence

Each analyst writes two artifacts: the rendered `<key>_report.md` as today, and
`<key>_assessment.json` holding the validated object verbatim. Without the sidecar
every new audit field is discarded at write time, since the checkpoint store records
one markdown file per analyst and re-validates it on resume.

Both must be present and valid for a checkpoint to count as complete; otherwise a
resumed run silently loses the audit trail for already-completed analysts. Rejected
attempts persist as `attempts/*.invalid.json` when a JSON object was parsed, and
`.invalid.txt` when the reply was not parseable at all.

`CHECKPOINT_SCHEMA_VERSION` bumps 3 -> 4. **Every existing manifest is invalidated,
including the in-flight study's**, so a resume after this change re-scores from
scratch. That is unavoidable: the artifact set per analyst has changed.

## Failure Semantics And Retry Accounting

**Amended after the final review.** This table first stated a budget of 3 transient
retries alongside a backoff of 0.5/1/2/4s. Those cannot both hold: three retries sleep
0.5, 1.0 and 2.0 seconds and total 3.5, leaving the 4.0s step unreachable. Four retries
make both statements true and raise total patience to 7.5s, which matters because the
acceptance gate is a 70-call paid run with three OpenRouter analysts sharing one key and
no `--continue-on-failure`; abandoning an analyst 3.5 seconds into a rate limit was the
worse reading. Honouring `Retry-After` is the better answer and is deliberately not done.


| Condition | Budget | Exhausted behaviour |
| --- | --- | --- |
| Invalid/unparseable JSON, schema violation | `max_retries` (default 2) | `validation` failure |
| 429, 5xx, timeout, connection error | 4, backoff 0.5/1/2/4s + jitter | `execution` failure |
| `finish_reason` says length | 1 doubling of `max_output_tokens` | `truncation` failure |
| Provider rejects the response format | step down the 3-tier ladder | last tier carries no format; a recurring 400 is terminal |
| 400, 401, 403, 404, 422 | none | `terminal` failure, immediately |

The budgets are independent. This is the most important behavioural change after the
executor removal: today a 429 lands in a bare `except Exception` at `crew.py:370`, is
classified as an execution failure, and consumes one of only three attempts. There is
no backoff and no rate-limit handling anywhere in `src/`.

`FailureKind` grows from `Literal["validation", "execution"]` to include `truncation`
and `terminal`; `validate_failure_kind`'s allowlist and `_VALIDATION_FIELD_PATHS` (a
closed `frozenset` that raises on any unknown path) both open to the new schema paths.
Both strings are persisted, in `analyst_checkpoints.json` and `run_status.json`, which
is part of why the schema version bumps.

**Retry accounting.** Because transient retries and mode downgrades deliberately do
not append to the attempt list, a counter derived from attempts cannot see them, and
the acceptance gate would be unmeasurable. The manifest therefore records four
counters per analyst: `validation_retries`, `transient_retries`,
`truncation_escalations`, `mode_downgrades`, plus the final `mode` reached. The gate
counts **validation retries only**; the other three are reported beside it. This is
also what makes the eta/OpenRouter hypothesis testable -- a persistent
`mode_downgrades: 2` on eta would confirm it independently of its retry total.

## Testing Strategy

- A `FakeProvider` implementing `CompletionProvider` drives every runner test. No test
  makes a live API call, per AGENTS.md.
- **No live call anywhere in the build**, not merely in the test suite. This is now a
  hard constraint rather than a preference, and it bounds what the build can prove:
  provider adapters are exercised against recorded transport-level responses and a
  fake transport, which demonstrates that the request we construct is the request we
  intended, not that the vendor accepts it. See **Hand-Off**.
- `sleep` is injected into `run_contract` so backoff paths run instantly without
  changing retry-count semantics.
- Per-budget tests: a transient error does not consume a validation attempt; three do
  fail; a truncated reply doubles the cap once then fails; a 400 steps down the ladder
  and a recurring 400 is terminal; a 401 fails immediately.
- Request-builder tests assert each provider fact in the table above, one test per
  row, each naming the symptom it prevents.
- A roster test proves a one-analyst run produces a report, a sidecar, a workbook
  column and an aggregate row.
- A test asserts the literal D8 table matches the code constant.
- A test asserts `g1` recomputed from the eight cause flags matches the reported
  value, and that a mismatch is rejected.
- A resume test proves a checkpoint missing its sidecar is not treated as complete.
- The shared-core byte-identity test, the item 6 cascade tests and the day-numbering
  tests must pass unchanged -- they cover the frozen region.

## Acceptance Criteria

The builder executes no live API call, so the criteria split in two. The first group
is the definition of done for the build. The second is run by the user afterwards and
is the definition of done for the change.

### Verified by the builder, offline

1. `crewai` and `litellm` appear nowhere in `src/` or `pyproject.toml`, and
   `grep -rn "kickoff" src/` returns nothing.
2. The suite passes, with roughly 100-150 tests rewritten or deleted as described
   above. The frozen-region tests pass unchanged; the three named output-contract
   tests are rewritten.
3. The default one-analyst roster completes a full run end to end against
   `FakeProvider`: report, sidecar, workbook score column, aggregate row.
4. The seven-analyst study roster produces today's artifact layout for all seven, plus
   sidecars, and a three-analyst roster reproduces today's default topology -- so the
   behaviour that used to be the default is still covered, just no longer by default.
5. Every provider fact in the table above has a request-builder test that names the
   symptom it prevents.
6. `rules_version` is still `RUCAM_DananBenichou1993_modified_sheet_v3` and the diff
   on the shared core region is empty.
7. `AGENTS.md` (§2 diagram, §6 roster, §9 Definition Of Done) and `README.md`
   describe the declared roster and the one-analyst default, not the removed env
   contract or the three-analyst topology.

### Verified by the user, live, in this order

8. **Smoke.** `--analysts config/analysts.smoke.json`, one PDF, one repeat: five
   calls. Passes when all five analysts return a validated assessment and
   `mode_downgrades` is 0 for each. This is where a wrong provider fact surfaces, and
   five calls is a cheap place to find it.
9. **Gate.** `--analysts config/analysts.study.json` over the two study PDFs x five
   repeats. Passes at **fewer than 5 validation retries across 70 analyst runs**, down
   from 31, with no single analyst above 1. Transient, truncation and downgrade counts
   are reported alongside rather than folded in.
10. No `Missing SECTION` rejection occurs in either run, because sections are no
    longer the model's responsibility.

Criteria 8-10 stay open at hand-off. The build is not claimed to be measured until the
user reports them.

## Hand-Off: What The Builder Cannot Verify

A green suite will not mean a working run, and the hand-off has to say so rather than
let the test count imply otherwise. Three things reach the user unverified:

- **Every provider request shape.** The nine facts in the provider table are asserted
  against a fake transport. A renamed parameter or a wrong `extra_body` key passes
  offline and fails on the first real call. Step 8 exists for exactly this and should
  run before the 70-call gate, not after it.
- **The eta/OpenRouter hypothesis.** `mode_downgrades` on `analyst_eta` is the
  measurement that confirms or kills it, and that counter only moves in a live run.
- **Whether the token budget still fits.** `finish_reason` handling is tested against
  a synthetic length stop, but whether 32000 output tokens accommodate the enlarged
  schema on a reasoning model is a live fact. The truncation counter is the signal; a
  non-zero `truncation_escalations` on the smoke run means the default budget needs
  raising before the gate is worth paying for.

The hand-off ships the two commands of steps 8 and 9 verbatim, together with the four
counter columns to read out of each manifest.

## Deferred

- **Collapsing the strict and inferring variants.** DILIgence returns `score` and
  `score_strict` per item from one call, which would eliminate our two prompt files,
  the byte-identity sync test, and half the calls needed for both modes. It is clearly
  right, and deliberately not here: it alters the frozen core, and bundling it would
  make a score movement unattributable. It follows the first clean measurement.
- Analyst `gather` fan-out with per-provider caps.
- Repeat and PDF parallelism.
- Per-item columns in the reproducibility workbook.
- An arbiter pass.

## Decisions Taken

Both outstanding questions are answered, and recorded here because each one changed
the design rather than merely assigning it.

1. **The widened scope is confirmed.** The `crewai` dependency entry goes now, which
   pulls in the ground-truth score finder, roughly 100-150 test rewrites, the
   `AGENTS.md` and `README` update, and a checkpoint schema bump that invalidates
   every existing manifest including the in-flight study's.
2. **The user executes every live call.** The builder stops at "ready to measure".
   This is not just a hand-off detail: it means the build cannot confirm a single
   provider request against a real endpoint, which is why the acceptance criteria are
   split, why the five-call smoke roster exists ahead of the 70-call gate, and why
   **Hand-Off** states what remains unverified.
3. **The default roster is one analyst.** Development needs neither seven nor the
   three that are the default today. The seven become an explicit file used for the
   gate, where comparability with the measured baseline requires them, and
   `AGENTS.md`'s definition of a valid run is rewritten in terms of the declared
   roster. The iteration loop makes no live calls at all.
