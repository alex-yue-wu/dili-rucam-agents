# Batched Reproducibility Analysis Design

**Date:** 2026-08-22
**Status:** Approved in design review; awaiting written-spec review

## Purpose

Add an explicit reproducibility mode to the existing folder batch command. The mode
processes every PDF directly inside an input directory and runs the normal full RUCAM
analysis a configurable number of times for each PDF. Every repeat is isolated on
disk, resumable through the existing analyst checkpoint system, and represented in a
per-PDF reproducibility workbook.

The clinical analysis itself must not change. Reproducibility mode is orchestration
around the same `run_end_to_end` call, prompt selection, masking behavior, analyst
topology, validation, retry policy, persistence, and safe-diagnostic boundaries used
by regular analysis.

## Goals

- Add reproducibility mode to `scripts/run_batch.py`.
- Default to five repeats while allowing any positive repeat count.
- Process PDFs and repeats sequentially in deterministic order.
- Store every repeat in an independent `<pdf-stem>_<repeat>` directory.
- Preserve normal analyst retry and per-analyst resume behavior inside each repeat.
- Skip only compatible completed repeats on a later invocation.
- Generate a two-sheet `summary.xlsx` in each PDF's parent output directory.
- Stop at the first repeat that fails after normal analyst retries are exhausted.
- Preserve safe diagnostics in exceptions, status files, logs, and workbooks.
- Keep regular batch behavior and its root `batch_summary.xlsx` unchanged.

## Non-Goals

- Changing RUCAM prompts, scoring rules, ingestion, masking, analyst models, report
  validation, or analyst retry limits.
- Running PDFs, repeats, or analysts concurrently.
- Sharing analyst reports or checkpoints between different repeats.
- Copying source PDFs into the output directory.
- Recursively discovering PDFs below the input directory.
- Migrating or automatically importing the historical
  `results/results_reproducibility` directory structure.
- Writing a global reproducibility workbook at the output root.
- Adding inferential statistical tests or cross-analyst consensus metrics.

## User Interface

Regular batch mode remains:

```bash
uv run python scripts/run_batch.py INPUT_DIR OUTPUT_DIR [existing flags]
```

Reproducibility mode is activated explicitly:

```bash
uv run python scripts/run_batch.py INPUT_DIR OUTPUT_DIR \
  --reproducibility \
  --repeats 5 \
  [existing flags]
```

`INPUT_DIR` and `OUTPUT_DIR` remain the first and second positional arguments for
backward compatibility. All existing analysis flags remain available, including the
prompt override, score masking, strict scoring, optional analysts, debug logging,
force rerun, and analyst restart limit.

The command-line rules are:

- `--reproducibility` selects the new output and orchestration contract.
- In reproducibility mode, the effective repeat count defaults to `5`.
- `--repeats N` accepts integers where `N >= 1`.
- Supplying `--repeats` without `--reproducibility` is a parser error. The parser
  therefore needs to distinguish an omitted repeat argument from the effective
  reproducibility default.
- Regular batch mode continues to call `run_batch_folder` and produce its current
  output layout.
- Reproducibility mode calls a new public Python helper,
  `run_reproducibility_folder`, and prints the ordered per-PDF summary paths after a
  successful run.

The public helper mirrors the existing `run_batch_folder` analysis arguments, adds
`repeats: int = 5`, and returns `list[Path]` containing each generated per-PDF
`summary.xlsx` path in PDF processing order. An input folder with no matching PDFs
returns an empty list and produces no reproducibility workbook, consistent with the
existing non-recursive batch discovery behavior.

## Output Contract

Given `INPUT_DIR/case-a.pdf`, `INPUT_DIR/case-b.pdf`, `OUTPUT_DIR`, and three
repeats, output is:

```text
OUTPUT_DIR/
  case-a/
    summary.xlsx
    case-a_1/
      run_status.json
      analyst_checkpoints.json
      analyst-alpha_report.md
      analyst-beta_report.md
      analyst-gamma_report.md
      ...optional workflow artifacts...
    case-a_2/
      ...normal single-analysis artifacts...
    case-a_3/
      ...normal single-analysis artifacts...
  case-b/
    summary.xlsx
    case-b_1/
      ...normal single-analysis artifacts...
    case-b_2/
      ...normal single-analysis artifacts...
    case-b_3/
      ...normal single-analysis artifacts...
```

The PDF parent directory is `OUTPUT_DIR / pdf_path.stem`. Repeat directories are
`OUTPUT_DIR / pdf_path.stem / f"{pdf_path.stem}_{repeat_index}"`, with one-based
indices. A repeat directory contains the artifacts produced by one normal
`run_end_to_end` analysis. It does not contain a nested `batch_summary.xlsx`.

Reproducibility mode does not copy the source PDF and does not write the regular
root-level `batch_summary.xlsx`. Existing files and repeat directories outside the
currently requested repeat range are not deleted. For example, rerunning with three
repeats after an earlier five-repeat run summarizes repeats 1 through 3 and leaves
directories 4 and 5 untouched.

## Architecture

The implementation will use three layers:

1. **CLI dispatch:** the existing batch parser selects regular or reproducibility
   orchestration while retaining the two positional directory arguments and all
   existing analysis flags.
2. **Shared single-PDF execution:** the status, completion check, debug logging,
   `run_end_to_end` call, report parsing, safe failure conversion, and result-row
   creation currently embedded in `run_batch_folder` become one reusable internal
   unit. Both regular and reproducibility orchestration call this unit.
3. **Reproducibility orchestration:** a focused module creates the nested directory
   layout, invokes the shared execution unit once per repeat, refreshes the per-PDF
   workbook, and enforces fail-fast ordering.

This design makes the shared single-PDF execution unit the behavioral boundary.
Reproducibility mode does not reproduce or fork clinical pipeline logic. Regular
batch mode continues to use the same behavior after the extraction, so its current
validation, checkpoint, status, logging, and failure tests remain authoritative.

The shared unit returns the validated summary data for a completed or reused run.
On failure, it writes the repeat's failed `run_status.json` and raises a bounded
internal failure carrying only the safe diagnostic and any summary values recovered
from already validated canonical reports. The caller decides which workbook to
refresh before re-raising a public `RuntimeError`.

## Processing Flow

Reproducibility processing is deterministic and sequential:

1. Resolve the input and output directories using the same path rules as regular
   batch mode.
2. Resolve enabled analysts and their models once for the invocation.
3. Discover direct-child `*.pdf` files and sort them using the existing filename
   ordering.
4. For each PDF, create `OUTPUT_DIR/<pdf-stem>`.
5. For repeat indices `1..N`, derive the repeat directory and call the shared
   single-PDF execution unit with all analysis settings unchanged.
6. After a completed or compatible reused repeat, add its run record and atomically
   refresh `summary.xlsx`.
7. If a repeat fails, recover any available valid canonical report scores, append a
   failed run record, atomically refresh `summary.xlsx`, and stop immediately.
8. Continue to the next PDF only after all requested repeats for the current PDF
   complete or are compatibly reused.

There is no reuse between repeat directories. Calling the pipeline once in
`case-a_1` and once in `case-a_2` performs two independent full analyses, including
deterministic ingestion and masking when enabled. Resume is local to a repeat: it
may reuse that repeat's compatible validated analyst checkpoints, but it never reads
another repeat's reports.

## Status, Resume, and Force Rerun

Every repeat uses its own `run_status.json`. The existing fields remain, and
reproducibility mode adds:

```json
{
  "mode": "reproducibility",
  "repeat_index": 1
}
```

The normal `running`, `completed`, and `failed` transitions remain unchanged. A
repeat is eligible for the fast skip only when its completed status matches the PDF
and analysis settings and `is_end_to_end_complete` confirms every enabled artifact
and checkpoint is compatible and valid.

On an ordinary rerun:

- compatible completed repeats are parsed into the workbook without invoking the
  clinical pipeline;
- failed, incomplete, legacy, or incompatible repeats enter `run_end_to_end` with
  resume enabled;
- compatible analyst reports within that repeat may be reused;
- processing resumes in PDF-name and repeat-index order.

`--force-rerun` bypasses completion skipping and calls `run_end_to_end` with resume
disabled for every requested repeat. It retains the existing non-destructive force
behavior: the command does not delete repeat directories or audit history before
execution.

## Failure Semantics

Reproducibility mode is fail-fast, matching regular batch behavior. Analyst-level
restarts still occur according to `--analyst-restarts`. If the repeat remains
unsuccessful after those attempts:

1. its status becomes `failed` using the existing safe diagnostic renderer;
2. validated scores already persisted for that repeat are collected when possible;
3. the current PDF's `summary.xlsx` is refreshed with a failed row;
4. a public `RuntimeError` identifies the PDF and repeat using only the safe
   diagnostic; and
5. no later repeat or PDF starts.

Summaries already written for earlier PDFs remain valid. Re-running the same command
revisits completed repeats through the compatibility gate and resumes at the failed
or incompatible repeat.

No raw provider exception message, credential, request body, invalid model output,
or clinical text may be copied into `run_status.json`, debug logs, workbook cells,
or public exception strings. Invalid model output remains confined to the existing
audited `attempts/` artifacts.

## Per-PDF Summary Workbook

Each PDF parent directory contains `summary.xlsx`. It is regenerated from the run
records accumulated for the currently requested repeat range after every completed,
reused, or failed repeat. Workbook writes use a temporary file in the same directory
followed by atomic replacement so interruption cannot leave a partially written
summary.

### `Runs` sheet

The `Runs` sheet contains one row per visited repeat and uses these columns:

1. `repeat`
2. `repeat_directory`
3. `status`
4. `pdf_filename`
5. one `<analyst_key>_score` column per enabled analyst, in configured analyst order
6. `masked_rucam_score` when score masking is enabled
7. `masked_rucam_category` when score masking is enabled
8. `error`

`status` is `completed` or `failed`; a compatible skipped repeat remains a completed
analysis. `error` is blank for completed rows and contains only the safe diagnostic
for a failed row. Scores are numeric workbook values. Missing values are blank, not
zero and not the string `None`.

Stable analyst-key score headers avoid ambiguity when two analyst configurations
resolve to the same model. The `Reproducibility` sheet records the analyst-to-model
mapping.

### `Reproducibility` sheet

The aggregate sheet contains one row per enabled analyst, in configured order, and
one additional `ground_truth_rucam` row when score masking is enabled. Its columns
are:

1. `scorer`
2. `model`
3. `valid_repeats`
4. `mean`
5. `sample_standard_deviation`
6. `minimum`
7. `maximum`
8. `range`
9. `mode`
10. `exact_mode_agreement`

Only numeric scores from rows whose status is `completed` contribute to statistics.
Failed repeats remain on `Runs`, where scores recovered from already validated
canonical reports may be shown for auditability, but no value from a failed row
contributes to `Reproducibility`.

Statistics are defined as follows:

- `valid_repeats`: number of numeric scores for the scorer.
- `mean`: arithmetic mean.
- `sample_standard_deviation`: sample standard deviation with denominator `n - 1`;
  blank when fewer than two values exist.
- `minimum` and `maximum`: smallest and largest score.
- `range`: `maximum - minimum`.
- `mode`: the sorted modal score values; a single number when unique and a
  comma-separated text list when tied.
- `exact_mode_agreement`: highest score frequency divided by `valid_repeats`, stored
  as a numeric proportion and formatted as a percentage.

When no valid values exist, all aggregate fields except `scorer`, `model`, and
`valid_repeats` are blank. The ground-truth model cell follows the existing agent's
exact resolution chain: `GROUND_TRUTH_SCORE_FINDER_MODEL`, then `OPENAI_MODEL`, then
`gpt-5.4`.

## File Responsibilities

### `src/dili_rucam_agents/batch.py`

- Retain the regular batch public API and workbook behavior.
- Extract the shared single-PDF execution boundary from the existing loop.
- Expose bounded completion/failure results for both orchestrators.
- Extend `_main` with reproducibility dispatch and validated repeat arguments.
- Keep existing safe diagnostic and legacy summary parsing behavior.

### `src/dili_rucam_agents/reproducibility.py`

- Provide `run_reproducibility_folder`.
- Create per-PDF and per-repeat paths.
- Invoke the shared execution boundary in deterministic order.
- Refresh and atomically persist each per-PDF workbook.
- Calculate aggregate statistics with standard-library numeric/statistics helpers.
- Enforce fail-fast behavior after the failed workbook row is persisted.

### `scripts/run_batch.py`

- Remain the user-facing script and delegate to the extended batch `_main` entry
  point. No second reproducibility script is added.

### `tests/test_reproducibility.py`

- Contain focused tests for reproducibility layout, orchestration, summaries,
  resume, force rerun, failure behavior, diagnostic safety, and CLI dispatch.

### `tests/test_batch.py`

- Retain the regular batch regression tests.
- Receive only changes required by extracting the shared single-PDF boundary.

### `README.md`

- Document the new command, default and valid repeat counts, output tree, workbook
  sheets and metrics, sequential/fail-fast execution, resume behavior, force rerun,
  and the difference from regular batch output.

No new runtime dependency is required. Workbook generation continues to use the
repository's existing `openpyxl` dependency.

## Testing Strategy

All feature tests are offline and replace live model execution with controlled test
doubles. Coverage includes:

1. CLI dispatch for regular versus reproducibility mode.
2. Default repeat count of five.
3. Rejection of zero, negative, non-integer, or regular-mode-only `--repeats` use.
4. Multiple PDFs and repeats executing in sorted PDF/index order.
5. Exact `<pdf-stem>/<pdf-stem>_<index>` paths.
6. Absence of redundant per-repeat `batch_summary.xlsx` files.
7. Forwarding every existing analysis option unchanged.
8. Per-repeat `run_status.json` mode and repeat metadata.
9. Compatible completed-repeat skipping without clinical pipeline execution.
10. Failed/incomplete repeat resume and topology/configuration compatibility.
11. Repeat-count expansion and contraction without deleting unrelated directories.
12. `--force-rerun` bypassing skip and pipeline resume for all requested repeats.
13. `Runs` sheet ordering, statuses, numeric scores, masking columns, and blanks.
14. Aggregate mean, sample standard deviation, min, max, range, unique mode, tied
    modes, exact-mode agreement, and zero/one-value cases.
15. Masked ground-truth score aggregation when masking is enabled.
16. Failed repeats excluded from statistics even when an already validated canonical
    report provides a partial score for the `Runs` sheet.
17. Fail-fast behavior preventing later repeats and PDFs from starting.
18. Workbook refresh before a failure is raised.
19. Diagnostic safety across public exceptions, statuses, debug logs, and workbook
    cells using credential-like and clinical sentinel strings.
20. Debug logs created inside each repeat only when debug mode is enabled.
21. Existing regular batch output, resume, failure, workbook, and diagnostic tests
    continuing to pass unchanged in behavior.

Verification consists of targeted reproducibility and batch tests, the relevant
checkpoint and report-validator suites, the full offline test suite, formatting,
linting of changed files, and `git diff --check`. No live provider calls are part of
verification.

## Acceptance Criteria

The feature is complete when:

- the existing batch script accepts the approved reproducibility command;
- every requested PDF/repeat executes the unchanged clinical analysis in its own
  directory;
- repeat outputs use the approved naming contract;
- compatible repeats skip and failed repeats resume independently;
- fail-fast behavior persists the current per-PDF summary before stopping;
- every completed PDF has a correct two-sheet `summary.xlsx`;
- aggregate calculations match independently computed test expectations;
- regular batch mode remains backward compatible;
- safe diagnostic guarantees hold for every new persistence and terminal boundary;
  and
- all offline targeted and full regression checks pass.
