# DILI RUCAM Agents

Production-grade CrewAI implementation for multi-analyst RUCAM causality assessment of drug-induced liver injury (DILI) case report PDFs.

## Purpose

This system performs production-grade RUCAM causality assessment from published clinical case report PDFs with variable layouts, tables, and figures.

Core guarantees:

- Deterministic, auditable PDF ingestion
- Optional deterministic score masking to suppress prior RUCAM conclusions in the source text
- Independent multi-analyst RUCAM scoring
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
 ┌───────────────────────────┐
 │ Analyst Alpha             │
 ├───────────────────────────┤
 │ Analyst Beta              │
 ├───────────────────────────┤
 │ Analyst Gamma             │
 └───────────────────────────┘
          ↓
 optional Analyst Delta / Epsilon / Zeta / Eta
          ↓
 Independent RUCAM reports + JSON
```

## Repository Layout

- `agent.md` and `AGENTS.md` - canonical specification and entry point.
- `docs/` - design docs and execution plans.
- `src/dili_rucam_agents/ingestion/` - deterministic PDF ingestion stack.
- `src/dili_rucam_agents/masking.py` - deterministic score masking utilities.
- `src/dili_rucam_agents/prompts/` - production RUCAM prompt.
- `src/dili_rucam_agents/crew/` - CrewAI agents, tasks, and crew builder.
- `src/dili_rucam_agents/pipeline.py` - CLI/SDK entry point.
- `src/dili_rucam_agents/validators/` - schema enforcement for SECTION C JSON.
- `tests/` - ingestion, masking, routing, and topology tests.

## Quickstart

```bash
uv sync
export OPENAI_API_KEY=...
export GEMINI_API_KEY=...
export ANTHROPIC_API_KEY=...
export DEEPSEEK_API_KEY=...
export OPENROUTER_API_KEY=...

# default: 3 analysts; up to two restarts after an initial attempt
uv run python -m dili_rucam_agents.pipeline \
    examples/3568943.pdf \
    --output-dir results/3568943 \
    --analyst-restarts 2

# enable score masking and all 4 optional analysts, and persist reports
uv run python -m dili_rucam_agents.pipeline \
    examples/3568943.pdf \
    --output-dir examples/latest_reports \
    --mask-scores \
    --analyst-delta \
    --analyst-epsilon \
    --analyst-zeta \
    --analyst-eta

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

Environment variables let you pin each model deterministically:

| Variable | Purpose | Default Fallback |
| --- | --- | --- |
| `INGESTION_MODEL` | Override the ingestion helper model. | `OPENAI_MODEL` or `gpt-5.4-mini` |
| `MASKING_MODEL` | Reserved for the masking step configuration path. | `OPENAI_MODEL` or `gpt-5.4-mini` |
| `ANALYST_ALPHA_MODEL` | Analyst Alpha model override. Falls back to `ANALYST_MODEL` then `OPENAI_MODEL`. | `gpt-5.5` |
| `ANALYST_BETA_MODEL` | Analyst Beta model override. Falls back to `ANALYST_MODEL`, `GEMINI_MODEL`, `OPENAI_MODEL`. | `gemini-3.1-pro-preview` |
| `ANALYST_GAMMA_MODEL` | Analyst Gamma model override. Falls back to `ANALYST_MODEL`, `ANTHROPIC_MODEL`, `OPENAI_MODEL`. | `moonshotai/kimi-k2.5` |
| `ANALYST_DELTA_MODEL` | Optional Analyst Delta model override. | `deepseek-v4-pro` |
| `ANALYST_EPSILON_MODEL` | Optional Analyst Epsilon model override. | `qwen/qwen3.5-plus-02-15` |
| `ANALYST_ZETA_MODEL` | Optional Analyst Zeta model override. | `claude-opus-4-7` |
| `ANALYST_ETA_MODEL` | Optional Analyst Eta model override. | `z-ai/glm-5` |
| `ANALYST_ALPHA_MAX_TOKENS` | Analyst Alpha max completion/output tokens. Falls back to `ANALYST_MAX_TOKENS`, then `LLM_MAX_TOKENS`. | `12000` |
| `ANALYST_BETA_MAX_TOKENS` | Analyst Beta max completion/output tokens. Falls back to `ANALYST_MAX_TOKENS`, then `LLM_MAX_TOKENS`. | `12000` |
| `ANALYST_GAMMA_MAX_TOKENS` | Analyst Gamma max completion/output tokens. Falls back to `ANALYST_MAX_TOKENS`, then `LLM_MAX_TOKENS`. | `12000` |
| `ANALYST_DELTA_MAX_TOKENS` | Analyst Delta max completion/output tokens. Falls back to `ANALYST_MAX_TOKENS`, then `LLM_MAX_TOKENS`. | `12000` |
| `ANALYST_EPSILON_MAX_TOKENS` | Analyst Epsilon max completion/output tokens. Falls back to `ANALYST_MAX_TOKENS`, then `LLM_MAX_TOKENS`. | `12000` |
| `ANALYST_ZETA_MAX_TOKENS` | Analyst Zeta max completion/output tokens. Falls back to `ANALYST_MAX_TOKENS`, then `LLM_MAX_TOKENS`. | `12000` |
| `ANALYST_ETA_MAX_TOKENS` | Analyst Eta max completion/output tokens. Falls back to `ANALYST_MAX_TOKENS`, then `LLM_MAX_TOKENS`. | `12000` |
| `ANALYST_MAX_TOKENS` | Shared fallback max completion/output tokens for all analysts. | unset |
| `LLM_MAX_TOKENS` | Global fallback max completion/output tokens. | unset |
| `ANALYST_MODEL` | Shared fallback for analysts without their own override. |  |
| `OPENAI_MODEL`, `GEMINI_MODEL`, `ANTHROPIC_MODEL` | Legacy provider-specific fallbacks. |  |

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

- `analyst-alpha_report.md`
- `analyst-beta_report.md`
- `analyst-gamma_report.md`
- `analyst-delta_report.md`
- `analyst-epsilon_report.md`
- `analyst-zeta_report.md`
- `analyst-eta_report.md`

- Once analyst execution starts, `analyst_checkpoints.json` records the
  per-analyst checkpoint state, compatible fingerprint, completed-report filename,
  attempt counts, and failure diagnostics.

- `attempts/` contains invalid model outputs as
  `analyst-<name>_attempt-<cumulative-number>-<unique-id>.invalid.md`. Names are
  cumulative and collision-resistant, so a later invocation cannot overwrite an
  earlier invalid output. An execution exception without a report has no markdown
  artifact. Public attempt callbacks receive only validated structured
  diagnostics: execution failures contain a conservative exception type and
  allowlisted numeric identifiers, while validation failures contain only
  allowlisted field paths and issue codes rendered through fixed messages. Raw
  exception objects remain local for terminal exception chaining, and raw invalid
  report text is handed only to the invalid-attempt audit writer. Persistence
  boundaries validate and render both structures again without trusting
  caller-supplied text, arbitrary objects, raw provider messages, or request data.

Only enabled workflow artifacts are persisted.

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
`--reproducibility`. Every other batch flag (`--prompt-path`, `--mask-scores`,
`--strict-scoring`, the optional-analyst flags, `--debug`, `--force-rerun`, and
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
ascending repeat index. Execution is fail-fast: when a repeat fails, that failed
repeat is recorded in the current PDF's `summary.xlsx`, the run stops immediately,
and no later repeat or PDF is started.

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

Validated analyst reports are checkpointed independently in each PDF result
directory. Re-running a single PDF with the same `--output-dir` or re-running a
batch reuses compatible completed analyst reports. A batch skips a PDF only when
its completed status and every enabled analyst checkpoint remain compatible and
validate; a failed or incomplete PDF resumes at the first analyst without a
compatible validated report. Compatible successful analysts are skipped before the
failed analyst is retried. A completed run from a larger analyst topology may be
reused by a smaller topology without rewriting the checkpoint manifest; expanding
the topology still runs any missing or incompatible current analyst.

PDF content, the effective versioned analyst instruction contract, masking mode,
strict-scoring mode, resolved model, or effective max-output-token changes
invalidate only the affected analyst checkpoint. The instruction contract covers
the production prompt, task wrapper and expected output, and analyst
role/goal/backstory. Runtime case-bundle content and transient retry diagnostics
are not part of that instruction hash (the source PDF hash is tracked separately).
Old output directories without `analyst_checkpoints.json` are handled as legacy
checkpoint candidates during a normal resume. A manifestless completed batch is
not eligible for the read-only batch fast-skip: it enters the pipeline once, where
complete reports that pass the strict canonical validator are adopted into a
current versioned manifest, provided any existing `run_status.json` matches the PDF
filename, masking mode, and strict-scoring mode. A later compatible batch may then
skip normally. Invalid or incompatible legacy reports are run again.

Current checkpoint manifests use schema v3. Schema-v2 manifests predate the
structured diagnostic boundary and can contain arbitrary validation-error text, so
they are strictly incompatible: read-only completion does not skip or rewrite
them, and a normal resume reruns analysts before replacing the file with a clean v3
manifest. No schema-v2 diagnostic or attempt history is migrated.

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

## Tests

Run the regression-oriented test suite with:

```bash
uv run pytest tests/test_ingestion.py tests/test_bundle_schema.py tests/test_rucam_json_validator.py tests/test_agents.py tests/test_masking.py tests/test_crew_topology.py
```

This covers deterministic ingestion, JSON validation, provider routing, score masking behavior, and the new crew topology.
