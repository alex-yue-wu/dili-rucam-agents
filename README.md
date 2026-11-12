# DILI RUCAM Agents

Production-grade implementation, calling the vendor SDKs directly, for multi-analyst RUCAM causality assessment of drug-induced liver injury (DILI) case report PDFs.

## Purpose

This system performs production-grade RUCAM causality assessment from published clinical case report PDFs with variable layouts, tables, and figures.

Core guarantees:

- Deterministic, auditable PDF ingestion
- Optional deterministic score masking to suppress prior RUCAM conclusions in the source text
- Independent RUCAM scoring by the analysts declared in a roster file
- Strict structured JSON outputs suitable for automation

This system is decision-support only, not a medical diagnosis tool.

## High-Level Architecture

```text
PDF
 └─ Ingestion (deterministic, no LLM)
     ├─ unstructured (layout-aware blocks)
     ├─ pdfplumber (tables)
     └─ PyMuPDF fallback (adaptive 1/2/3-column)
     └─ Docling full-page OCR fallback (last resort only)
          ↓
     case_bundle (canonical JSON)
          ↓
     optional deterministic score masking
          ↓
     deterministic item 6 evidence cascade (no LLM)
     ├─ DILIrank v2 (offline, pinned)
     ├─ DailyMed label sections (cached)
     └─ PubMed case reports (cached, date-bounded)
          ↓
 analysts declared in the roster file
 (default config/analysts.json: one analyst)
          ↓
 Independent RUCAM reports + JSON
```

## Repository Layout

- `agent.md` and `AGENTS.md` - canonical specification and entry point.
- `docs/` - design docs and execution plans.
- `src/dili_rucam_agents/ingestion/` - deterministic PDF ingestion stack.
- `src/dili_rucam_agents/masking.py` - deterministic score masking utilities.
- `src/dili_rucam_agents/hepatotoxicity/` - deterministic RUCAM item 6 evidence cascade.
- `dilirank_v2/DILIrank_v2.xlsx` - pinned FDA DILIrank v2 workbook used by that cascade.
- `src/dili_rucam_agents/prompts/` - production RUCAM prompt.
- `config/` - analyst roster files: `analysts.json` (default, one analyst), `analysts.study.json` (seven) and `analysts.smoke.json` (five, one per provider adapter).
- `src/dili_rucam_agents/roster.py`, `providers.py`, `runner.py`, `analysts.py` - roster loading, the vendor-SDK adapters, the retrying call loop, and per-analyst execution.
- `src/dili_rucam_agents/pipeline.py` - CLI/SDK entry point.
- `src/dili_rucam_agents/validators/` - schema enforcement for SECTION C JSON.
- `tests/` - ingestion, masking, roster, provider, runner, checkpoint and docs-sync tests.

## Quickstart

```bash
uv sync
export OPENAI_API_KEY=...
export GEMINI_API_KEY=...
export ANTHROPIC_API_KEY=...
export DEEPSEEK_API_KEY=...
export OPENROUTER_API_KEY=...
# or put them in a .env file in the directory you run from

# default roster (config/analysts.json): one analyst; up to two restarts
uv run python -m dili_rucam_agents.pipeline \
    examples/3568943.pdf \
    --output-dir results/3568943 \
    --analyst-restarts 2

# the seven-analyst study roster, with score masking, persisting reports
uv run python -m dili_rucam_agents.pipeline \
    examples/3568943.pdf \
    --output-dir examples/latest_reports \
    --mask-scores \
    --analysts config/analysts.study.json

# batch mode over every PDF in a folder
uv run python scripts/run_batch.py \
    examples \
    results \
    --mask-scores \
    --analyst-restarts 2

# reproducibility mode: five independent full analyses per PDF
uv run python scripts/run_batch.py \
    examples \
    results/results_reproducibility \
    --reproducibility \
    --repeats 5 \
    --mask-scores \
    --analyst-restarts 2
```

Both CLIs read a `.env` file from the directory the command was run in (searching
upward); keys already exported take precedence. `--analysts <path>` selects the
roster; omitting it uses `config/analysts.json`, which declares **one** analyst.
This is a deliberate change from the former default of three, so a run that passes
no roster now produces one analyst's report.

## Docker

The repo includes a [Dockerfile](/Users/alexwu/Documents/GitHub/dili-rucam-agents/Dockerfile) that installs the system dependencies required by the ingestion stack, including `poppler` and `tesseract`.

Build:

```bash
docker build -t dili-rucam-agents .
```

Run a single PDF:

```bash
docker run --rm \
  -e OPENAI_API_KEY \
  -e GEMINI_API_KEY \
  -e ANTHROPIC_API_KEY \
  -e DEEPSEEK_API_KEY \
  -e OPENROUTER_API_KEY \
  -v "$PWD/examples:/data/input" \
  -v "$PWD/results:/data/output" \
  dili-rucam-agents \
  /data/input/3568943.pdf \
  --output-dir /data/output/3568943 \
  --mask-scores
```

Run batch mode:

```bash
docker run --rm \
  -e OPENAI_API_KEY \
  -e GEMINI_API_KEY \
  -e ANTHROPIC_API_KEY \
  -e DEEPSEEK_API_KEY \
  -e OPENROUTER_API_KEY \
  -v "$PWD/examples:/data/input" \
  -v "$PWD/results:/data/output" \
  --entrypoint uv \
  dili-rucam-agents \
  run python scripts/run_batch.py /data/input /data/output --mask-scores
```

## Configuration

Analysts are declared in a roster file, not in environment variables. Each entry
in `config/analysts.json` names a `key`, a `label`, an explicit `provider`
(`openai`, `anthropic`, `google`, `openrouter` or `deepseek`; never inferred from the
model string), a `model` and a `max_output_tokens` budget (default `32000`). The
model name is sent to the vendor SDK as written. The roster file also holds the
`ground_truth` scorer used with `--mask-scores`.

An entry may also declare `api_version`, the API surface its model is served on.
Only the google client takes one -- declaring it elsewhere is refused, not
ignored -- and no shipped roster declares one, so every entry is served on its
vendor default. The field exists because a model can be absent from one API
version and served on another: on 2026-11-11 `gemini-3.1-pro-preview` was
`404 not found for API version v1` on the google SDK's default and answered on
`v1alpha`, under a key revoked afterwards. If a preview model stops resolving,
this is the first thing to try:

```json
{
  "key": "analyst_beta",
  "label": "Analyst Beta",
  "provider": "google",
  "model": "gemini-3.1-pro-preview",
  "api_version": "v1alpha",
  "max_output_tokens": 32000
}
```

Checking a model name against a vendor's catalogue is not evidence that it can
be called. `gemini-2.5-pro` is returned by `models.list()`, resolves through
`models.get()` with `generateContent` among its supported actions, and refuses
every call as "no longer available to new users"; `gemini-3.1-pro-preview` is
absent from the same listing and works. Verify a new model by calling it.

API keys are the only analyst configuration read from the environment:

| Variable | Provider |
| --- | --- |
| `OPENAI_API_KEY` | `openai` |
| `ANTHROPIC_API_KEY` | `anthropic` |
| `GEMINI_API_KEY` | `google` |
| `OPENROUTER_API_KEY` | `openrouter` |
| `DEEPSEEK_API_KEY` | `deepseek` |

Before ingestion or any model call, every provider the run will use (the
roster's analysts, plus the ground-truth provider when masking is on) is key-
checked, and a failure names every missing variable at once. The former
`ANALYST_*_MODEL` and `*_MAX_TOKENS` variables no longer exist; change the roster
file instead.

## Docling Fallback

Docling full-page OCR now runs automatically as the last-resort ingestion fallback only when the default extraction stack returns no text blocks and no tables.

- It does not replace the default ingestion path.
- The current Docling fallback uses full-page OCR plus Docling table structure extraction.
- It is intended for hard PDFs like vector-only pages or scanned PDFs where `unstructured`, `PyMuPDF`, and `pdfplumber` all return no usable content.
- You still need the OCR backend available in the environment. The current fallback uses Tesseract through Docling's `TesseractCliOcrOptions`.

Validation warning:

- Complex multi-column journal layouts can cause column-order errors in Docling output.
- That risk is especially relevant for hepatology papers where scrambled ALT/AST timeline text can silently corrupt downstream scoring.
- Validate Docling output on a representative sample of your corpus before relying on the fallback broadly.

## Output Reports

When `--output-dir` is supplied, the pipeline writes artifacts for the enabled workflow:

- `masked-case-bundle_report.md` when `--mask-scores` is enabled. This is rendered from the same deterministic masked bundle JSON used for downstream analyst input, and includes a stable summary, extraction notes, masked text only, and masked table previews only.

- `hepatotoxicity-evidence_report.md` whenever the item 6 cascade runs. It states
  the cascade version, the as-of date, whether the networked tiers were consulted,
  one row per resolved drug, and the provenance behind each score (DILIrank row,
  DailyMed SPL ids, or the PubMed query and PMIDs).

- `<key>_report.md` and `<key>_assessment.json` for every analyst in the roster,
  with the key's underscores written as hyphens (for example
  `analyst-alpha_report.md` and `analyst-alpha_assessment.json`). The model returns
  one JSON object; Python renders Sections A, B and C from it and computes
  `total_score` and `category` from the seven item scores. A checkpoint counts
  complete only when both files are present and valid.

- Once analyst execution starts, `analyst_checkpoints.json` records the
  per-analyst checkpoint state, compatible fingerprint, completed-report filename,
  attempt counts, and failure diagnostics.

- `attempts/` contains rejected model replies as
  `<analyst_key>.<attempt-number>.<unique-id>.invalid.json` when a JSON object
  parsed but broke the schema, and `.invalid.txt` when nothing parsed. A reply cut
  off by the output budget is kept as
  `<analyst_key>.<attempt-number>.truncated<n>.<unique-id>.invalid.txt`. The
  attempt number restarts at 1 in every invocation, so the unique id is what makes
  the names collision-resistant: a later invocation cannot overwrite an earlier
  invalid output. An execution exception without a reply has no artifact at all.
  Public attempt callbacks receive only validated structured
  diagnostics: execution failures contain a conservative exception type and
  allowlisted numeric identifiers, while validation failures contain only
  allowlisted field paths and issue codes rendered through fixed messages. Raw
  exception objects remain local for terminal exception chaining, and raw invalid
  report text is handed only to the invalid-attempt audit writer. Persistence
  boundaries validate and render both structures again without trusting
  caller-supplied text, arbitrary objects, raw provider messages, or request data.

Only enabled workflow artifacts are persisted.

## Item 6 Evidence Cascade

RUCAM item 6 asks whether the drug's hepatotoxicity was already known. Answered
from model memory it is a reproducibility hazard, so it is resolved before any
analyst runs, by a deterministic cascade that stops at the first source which
resolves the drug:

| Order | Source | Resolves when | Points |
| --- | --- | --- | --- |
| 1 | DILIrank v2, pinned at `dilirank_v2/DILIrank_v2.xlsx` | the drug is listed | vMost/vLess-DILI-concern `+2`, Ambiguous `+1`, vNo `0` |
| 2 | the current FDA label on DailyMed | a label section reports liver injury | boxed warning or warnings & precautions `+2`, adverse reactions `+1` |
| 3 | PubMed case reports | the fixed query returns a report published on or before the as-of date | `+1`, otherwise `0` |

The result is attached to the case bundle as `hepatotoxicity_evidence`, and
Appendix C of the analyst prompt tells the analyst to read item 6 off that block
rather than recall it. A score it did not resolve falls back to the prompt's own
rungs, which require a labelling quote or a citation in the case text.

Drug names come from two places. Every DILIrank compound name occurring in the
case text is resolved automatically, which also means the table can list a drug
the paper only mentions in its discussion: the block is a lookup index, not a
list of suspects, and the analyst selects the row for the suspect drug it chose.
A drug DILIrank does not list reaches the DailyMed and PubMed tiers only when it
is named with `--drug`, because nothing else in the deterministic layer knows
which words are drug names.

```bash
# default: DILIrank for everything the case text names, no network
uv run python -m dili_rucam_agents.pipeline examples/3568943.pdf --output-dir results/x

# resolve a drug DILIrank does not list (herbals, withdrawn drugs, new approvals)
uv run python -m dili_rucam_agents.pipeline case.pdf --output-dir results/x \
    --drug "Ashwagandha" --hepatotoxicity-cache dilirank_v2/evidence-cache.json

# DILIrank only, never touch the network
uv run python -m dili_rucam_agents.pipeline case.pdf --output-dir results/x \
    --hepatotoxicity-offline

# turn the cascade off and score item 6 from the case text alone
uv run python -m dili_rucam_agents.pipeline case.pdf --output-dir results/x \
    --no-hepatotoxicity-evidence
```

### Drug-name matching

A case report names a drug the way its authors write it, which differs from US
reference data in four ways. All four are expanded into lookup keys by
`hepatotoxicity/names.py`, and the exact name is always tried before any
variant:

| Difference | Example | Handling |
| --- | --- | --- |
| salt, hydrate, formulation word | `Amiodarone hydrochloride`, `Tetracycline phosphate complex` | trailing modifier tokens stripped |
| British spelling | `sulphasalazine`, `oestradiol`, `amoxycillin` | spelling rules applied in both directions |
| INN against USAN | `paracetamol`, `ciclosporin`, `rifampicin`, `frusemide` | explicit one-to-one alias table |
| salt written first | `sodium valproate` | reordered to `valproate sodium` |

Measured against 31 forms as they appear in case reports, DILIrank lookup
resolves 28 (90%); before variant expansion it resolved 12 (38%). The benchmark
is `PUBLISHED_NAME_FORMS` in `tests/test_hepatotoxicity.py`, so a regression
fails a test rather than quietly lowering a score. No variant key merges two
different drugs, which is also asserted.

Known gaps, each deliberate:

- **Brand names** (`Augmentin`, `Tylenol`) are not in DILIrank, so the automatic
  text scan does not find them. Supplied with `--drug` they resolve fine, because
  DailyMed indexes brand names.
- **Drugs absent from DILIrank v2** reach the networked tiers only when supplied
  with `--drug`. Sulfamethoxazole has no row in version 2, and non-US agents such
  as flucloxacillin and nimesulide are absent by construction.
- **Combination products** (`co-amoxiclav`, `co-trimoxazole`) are not aliased to
  one ingredient: that would attribute the injury to the wrong moiety. Name the
  ingredient you are scoring.

DailyMed matches substrings of the names a label carries, so the salt-stripped
form finds more labels than the full one (62 against 58 for amiodarone) and a
British spelling finds none at all (`morphine sulphate` returns 0). The tier
therefore searches up to four variants in order and stops at the first that
returns a label, recording which name matched. The PubMed tier ORs the variants
into one query, so a paper using either spelling counts.

Each matched label set is read to a depth of three SPLs, chosen by ascending
setid. That depth was measured: one SPL misses the hepatic section for
cyclosporine and amiodarone, while three and five agree on every drug tried.

Reproducibility notes:

- The DILIrank tier is offline and needs no cache.
- The networked tiers are consulted only for `--drug` names, and every answer is
  written to `--hepatotoxicity-cache`. Commit that file and later runs reproduce
  the same scores without the network.
- `--hepatotoxicity-as-of` bounds the PubMed publication date, so a growing
  literature cannot move a score. Moving the date is a deliberate act, like
  bumping the rules version.
- The cascade settings are part of the analyst checkpoint fingerprint, so
  changing them re-runs the analysts instead of resuming stale reports.

## Batch Runs

Use [scripts/run_batch.py](/Users/alexwu/Documents/GitHub/dili-rucam-agents/scripts/run_batch.py) to process every `*.pdf` in an input folder.

- Each PDF is written to its own result subdirectory under the batch output folder, using the PDF stem as the directory name.
- The script writes `batch_summary.xlsx` in the batch output folder.
- The workbook includes:
  - the PDF filename in the first column
  - one column per enabled model, containing that model's computed total RUCAM score
  - `masked_rucam_score` and `masked_rucam_category` as the final columns when
    score masking is enabled

### Reproducibility mode

`--reproducibility` runs the same unchanged full analysis several times per PDF so
run-to-run score variation can be measured. It keeps the same two positional
arguments as a regular batch, `INPUT_DIR` and `OUTPUT_DIR`, and adds `--repeats N`.

`--repeats` defaults to `5`, must be an integer `>= 1`, and is rejected without
`--reproducibility`. `--continue-on-failure` is reproducibility-only and is
described below. Every other batch flag (`--prompt-path`, `--mask-scores`,
`--strict-scoring`, `--analysts`, `--debug`, `--force-rerun`, and
`--analyst-restarts`) behaves exactly as it does in a regular batch.

Each PDF gets one output directory named after its stem, holding one isolated
directory per repeat plus that PDF's summary workbook:

```
OUTPUT_DIR/
  case-a/
    case-a_1/          # full analysis artifacts for repeat 1
    case-a_2/
    ...
    summary.xlsx
  case-b/
    case-b_1/
    ...
    summary.xlsx
```

The source PDF is not copied, no nested `batch_summary.xlsx` is written inside a
repeat directory, and no reproducibility workbook is written at the output root.

PDFs run sequentially in sorted filename order, and each PDF's repeats run in
ascending repeat index. Execution is fail-fast by default: when a repeat fails,
that failed repeat is recorded in the current PDF's `summary.xlsx`, the run stops
immediately, and no later repeat or PDF is started. The abort names the failed
repeat and states that re-running the same command resumes the batch.

`--continue-on-failure` records the failed repeat and carries on with the
remaining repeats and PDFs instead, so one analyst that cannot produce a valid
report does not discard the repeats after it. Every visited repeat is still
written to its PDF's `summary.xlsx`, and the aggregate statistics cover the
repeats that completed. The run then exits with an error naming every failed
repeat, so a short batch is never reported as a clean one. The flag is rejected
without `--reproducibility`.

Checkpoint reuse is repeat-local. Each repeat directory carries its own analyst
checkpoints and its own `run_status.json` recording the reproducibility mode and
repeat index, so a completed repeat is skipped only when its own status matches
that same repeat; artifacts are never reused across repeats. Re-running with a
larger `--repeats` therefore executes only the newly added repeats, and re-running
with a smaller `--repeats` simply ignores the higher-numbered repeat directories
without deleting them. `--force-rerun` bypasses every checkpoint and completion
skip for that invocation, rerunning all requested repeats from scratch.

Each PDF's `summary.xlsx` is rewritten atomically after every visited repeat and
contains two sheets:

- `Runs` — one row per repeat, with the repeat index, repeat directory name,
  `completed`/`failed` status, PDF filename, one score column per enabled analyst,
  `masked_rucam_score` and `masked_rucam_category` when `--mask-scores` is on, and
  the safe error diagnostic for a failed repeat.
- `Reproducibility` — one aggregate row per enabled analyst, plus a
  `ground_truth_rucam` row when `--mask-scores` is on, reporting the scorer, its
  resolved model, `valid_repeats`, `mean`, `sample_standard_deviation` (Bessel-
  corrected; blank for fewer than two values), `minimum`, `maximum`, `range`,
  `mode` (tied modes are listed together), and `exact_mode_agreement` (the share of
  valid repeats equal to the mode).

Only `completed` repeats contribute to the aggregate statistics. A failed repeat is
excluded even when its `Runs` row shows partial validated analyst scores.

### Analyst retries and resume

Each analyst receives one initial attempt and, by default, up to two restarts when
execution fails or the report is incomplete. `--analyst-restarts` accepts `0`, `1`,
or `2`; its value counts restarts after the initial attempt, so the default permits
three total attempts per analyst in one invocation. The single-PDF and batch CLIs
both accept this option.

Four retry budgets are separate and never pooled:

- **validation retries** (default 2) for a reply that parsed but broke the schema;
- **transient transport retries** (4, with backoff 0.5/1/2/4s plus jitter, so
  total patience is 7.5s) for provider errors; the budget is per stall, and one
  reply resets it;
- **one truncation escalation**: a reply cut off by the output budget is reported
  as `report_truncated`, and the one retry that follows runs with a wider budget up
  to a ceiling of 64,000 tokens. On a reasoning model the budget covers hidden
  reasoning tokens as well as the visible report, so repeating a truncated attempt
  under the same budget only moves the cut point;
- **structured-output mode downgrades** along a `json_schema` -> `json_object` ->
  `text` ladder that starts at the highest mode each provider actually supports
  (`deepseek` has no native `json_schema` and starts at `json_object`).

Each analyst's entry in `analyst_checkpoints.json` records `validation_retries`,
`transient_retries`, `truncation_escalations` and `mode_downgrades`, plus the final
mode. The four counts are cumulative across invocations: a resumed run adds what it
spends to what the earlier run spent, so a resume that succeeds first time cannot
erase the retries that forced it. Each `attempt_history` entry written at the end
of an invocation also carries that invocation's own counts, so the total and the
breakdown both survive. The final mode is a state rather than a count, so it is the
last invocation's. The escalation is transient: the checkpoint is fingerprinted
against the configured `max_output_tokens`, not the escalated one. Changing the
configured budget does invalidate that analyst's checkpoints, which is deliberate,
because a report produced under a binding cap is not the same output as one
produced with room to finish.

Validated analyst reports are checkpointed independently in each PDF result
directory. Re-running a single PDF with the same `--output-dir` or re-running a
batch reuses compatible completed analyst reports. A batch skips a PDF only when
its completed status and every enabled analyst checkpoint remain compatible and
validate; a failed or incomplete PDF resumes at the first analyst without a
compatible validated report. Compatible successful analysts are skipped before the
failed analyst is retried. A completed run from a larger roster may be
reused by a smaller roster without rewriting the checkpoint manifest; expanding
the roster still runs any missing or incompatible current analyst.

PDF content, the effective versioned analyst instruction contract, masking mode,
strict-scoring mode, resolved model, or effective max-output-token changes
invalidate only the affected analyst checkpoint. The instruction contract covers
every field of the instruction contract, including the
three per-analyst persona fields (`agent_role`, `agent_goal`, `agent_backstory`),
which is why the instruction-contract hash differs between analysts; the analyst's model and
`max_output_tokens` are fingerprinted alongside it. Runtime case-bundle content and transient retry diagnostics
are not part of that instruction hash (the source PDF hash is tracked separately).
Old output directories without `analyst_checkpoints.json` are handled as legacy
checkpoint candidates during a normal resume. A manifestless completed batch is
not eligible for the read-only batch fast-skip: it enters the pipeline once, where
complete reports that pass the strict canonical validator are adopted into a
current versioned manifest, provided any existing `run_status.json` matches the PDF
filename, masking mode, and strict-scoring mode. A later compatible batch may then
skip normally. Invalid or incompatible legacy reports are run again.

Current checkpoint manifests use schema v4. A schema-v3 manifest is discarded
rather than migrated, so every manifest written before this version is
invalidated: read-only completion does not skip it, and a normal resume reruns the
analysts and replaces the file with a clean v4 manifest. No earlier diagnostic or
attempt history is carried over.

`--force-rerun` is a batch CLI control that bypasses every analyst checkpoint,
including legacy-output adoption, for that invocation. Invalid model outputs are
retained under `attempts/`, and `analyst_checkpoints.json` records per-analyst
status and attempt diagnostics.

New reports pass strict structural validation: they contain exactly one SECTION A,
SECTION B, and SECTION C in that order, with exactly one fenced JSON object in
SECTION C. Headings or JSON examples inside Markdown code fences do not count as
report structure. Legacy summary parsing remains deliberately tolerant and selects
the last historical fenced or unfenced JSON block when legacy mode is requested.
That tolerant parser exists only to rebuild summaries from historical output; it
does not authorize checkpoint adoption or a public `completed_reports` entry.
Malformed historical output is resumable only after it can be canonicalized into a
complete strict report without losing content.

## Acceptance Gate (live, paid, run by you)

The test suite is offline. Whether the analyst tier meets its retry baseline can
only be measured against the real providers, so it is a live, paid procedure that
you run yourself, in two stages.

**Stage 1: smoke.** Five calls over `config/analysts.smoke.json`, one per provider
adapter, on one PDF with one repeat. It is the cheapest run that touches every
transport.

```bash
uv run python scripts/run_batch.py --analysts config/analysts.smoke.json <one-pdf-dir> <out>
```

Read `validation_retries`, `transient_retries`, `truncation_escalations` and
`mode_downgrades` from each analyst's entry in `<out>/**/analyst_checkpoints.json`.
A non-zero `truncation_escalations` means the 32000-token default is too small for
the enlarged schema: raise `max_output_tokens` in the roster before paying for
stage 2.

**Stage 2: the gate.** `config/analysts.study.json` (seven analysts) over the two
study PDFs with five repeats is 70 analyst runs, the condition the retry baseline
was measured in.

```bash
uv run python scripts/run_batch.py --analysts config/analysts.study.json --reproducibility --repeats 5 <study-pdf-dir> <out>
```

Read the same four counter columns. The gate passes at fewer than 5 validation
retries across the 70 analyst runs, with none above 1 in any single run.

Two things about those numbers. They are **cumulative across invocations**, so if
a repeat fails and you re-run the command to resume it, the retries the failed
invocation spent are still counted — which is the point, since those are the runs
the gate most needs to see. For a repeat that completes in one sitting the number
is identical to a non-cumulative one, so the bar is read against the same
quantity it was set against. And `--force-rerun` does **not** reset them: it
re-scores into the same manifest, adding to the totals of the run it is
discarding. Point a forced repeat at a fresh output directory if you want its
numbers to be its own.

## Baseline shift in `masked_rucam_score`

The ground-truth prompt interpolates the configured model's name. The former code
passed the routed string (`anthropic/claude-opus-4-7`); the current code passes the
bare roster name (`claude-opus-4-7`). That one line of that prompt therefore differs
between old and new runs for every non-OpenAI provider. This is by design, not a
regression: the routing table is gone and the bare name is correct going forward.
Anyone comparing `masked_rucam_score` across this change should expect it.

## Tests

Run the suite with:

```bash
uv run pytest
```

It covers deterministic ingestion, masking, the roster, the provider adapters, the
retrying runner, checkpoints, rendering, and that these docs still describe the
declared-roster tier. None of it calls a live API.
