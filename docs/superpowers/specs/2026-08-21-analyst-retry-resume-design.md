# Analyst Retry and Resume Design

## Goal

Make batch execution resilient to incomplete analyst output while preserving successful analyst work. Each analyst may be restarted at most twice after its initial attempt, and a later batch invocation resumes a failed PDF from the first analyst without a compatible validated report.

## Current Behavior

The current pipeline already provides a partial checkpoint mechanism:

- Each successful non-empty analyst result is written immediately through the `on_report` callback.
- A later invocation loads analyst report files that contain fenced JSON with a `total_score` field.
- The isolated analyst loop skips keys supplied through `completed_reports`.
- Batch execution skips a PDF only when its run status is `completed` and all expected reports can be parsed.

This is insufficient because report completeness is checked only when the batch summary is populated. An incomplete but non-empty report can therefore be persisted as if it succeeded, later stop the batch, and be inconsistently classified during resume. `--force-rerun` also enters the pipeline without disabling analyst checkpoint reuse.

## Scope

This change covers:

- Immediate validation of every analyst report.
- Up to two restarts of only the analyst that produced an invalid result or raised during execution.
- Atomic persistence of validated reports.
- Per-analyst checkpoint metadata and compatibility checks.
- Resume of a failed PDF from its first incomplete analyst.
- Correct checkpoint bypass for `--force-rerun`.
- Tests and user documentation for retry and resume behavior.

This change does not:

- Run analysts concurrently.
- Continue to later PDFs after an analyst exhausts its attempts.
- Change the analyst model lineup or scoring prompts.
- Retry deterministic PDF ingestion failures.
- Add a database or external workflow engine.

## User-Visible Semantics

`max_restarts` defaults to `2` and is constrained to the range `0..2`. The initial execution is not a restart, so the default permits at most three total attempts for one analyst in one invocation.

The restart budget is per analyst and per invocation. Starting the batch script again gives the failed analyst a new restart budget, while compatible successful analyst checkpoints are reused without an LLM call.

Analysts remain sequential. If Alpha succeeds and Beta exhausts its attempts, Gamma is not run. On the next invocation, Alpha is skipped, Beta runs first, and Gamma runs only after Beta succeeds.

The batch remains fail-fast after the retry budget is exhausted. Its raised error identifies the PDF, analyst key, total attempts, and final validation or execution error.

## Architecture

### 1. Shared Analyst Report Validator

Create `src/dili_rucam_agents/validators/analyst_report.py` as the single boundary for determining whether an analyst report is complete.

The validator consumes report markdown and returns a typed result containing the parsed Section C payload. It raises a report-validation error with an actionable reason when any requirement fails.

A valid new report must contain:

- A non-empty Section A.
- A non-empty Section B.
- A Section C heading followed by one fenced JSON object.
- All required top-level Section C fields.
- All seven RUCAM score fields.
- A `total_score` equal to the sum of the seven item scores.
- A recognized causality category consistent with `total_score`: `Excluded` for scores at or below 0, `Unlikely` for 1–2, `Possible` for 3–5, `Probable` for 6–8, and `Highly probable` for 9 or above.

New analyst attempts require strict JSON. Existing tolerant extraction in `batch.py` remains available only for reading legacy reports and diagnostic recovery; it is not the success gate for newly generated output.

The RUCAM schema must be aligned with the production prompts before it is used at the boundary:

- `other_causes_excluded` is the canonical emitted field.
- `alternative_causes_excluded` is accepted as a legacy input alias.
- The allowed injury-pattern and R-ratio missing-value policy must match the production contract: `"Not reported"` and `null` are accepted when the case bundle lacks the evidence needed to calculate them.
- Canonical persisted payloads use `other_causes_excluded`.

`batch.py`, `pipeline.py`, and analyst retry handling all consume this validator. No component implements a separate definition of report success.

### 2. Fresh Analyst Attempt Construction

Refactor isolated analyst construction so one analyst configuration can create a fresh `Agent`, `Task`, and `Crew` for each attempt. Reusing a CrewAI task after a failed kickoff is avoided because task output and execution state may remain attached to the object.

The analyst-attempt helper performs:

1. Construct a fresh analyst run.
2. Call `kickoff` with the same canonical case bundle and model configuration.
3. Extract task output, falling back to kickoff output.
4. Validate the complete report.
5. Return the validated report, or record the attempt failure and restart when budget remains.

On a validation failure, the next attempt receives a short additional instruction containing only the validation reason and a reminder to return complete Sections A, B, and C. The invalid report itself is not fed back to the model, preventing accidental propagation of malformed content.

Execution exceptions from an analyst call are restartable. `KeyboardInterrupt`, `SystemExit`, deterministic ingestion failures, and configuration validation errors are not caught as analyst-attempt failures.

### 3. Report Persistence

The canonical report filename remains unchanged, such as `analyst-beta_report.md`.

The pipeline calls the persistence callback only after shared validation succeeds. Canonical reports are written atomically by writing a sibling temporary file and replacing the destination. An interrupted write therefore cannot leave a partial canonical report that appears reusable.

Invalid outputs are retained for auditability under the PDF output directory:

```text
attempts/
  analyst-beta_attempt-000001-<unique-id>.invalid.md
  analyst-beta_attempt-000002-<unique-id>.invalid.md
```

Attempt numbers are cumulative across invocations and filenames are collision-resistant, so reruns retain earlier invalid outputs. The manifest records append-only failure history with each artifact path. If an attempt raises before producing report text, no markdown attempt artifact is created; its exception is recorded in checkpoint metadata.

### 4. Analyst Checkpoint Manifest

Add `analyst_checkpoints.json` to each PDF output directory. The pipeline owns this file so standalone `run_end_to_end` execution and batch execution share identical resume behavior. `run_status.json` remains the batch-level status file.

The manifest has a versioned structure:

```json
{
  "schema_version": 2,
  "pdf_sha256": "...",
  "analysts": {
    "analyst_alpha": {
      "status": "completed",
      "report_file": "analyst-alpha_report.md",
      "fingerprint": "...",
      "attempts_in_last_invocation": 1,
      "total_attempts": 1,
      "completed_at": "...",
      "last_error": null
    }
  }
}
```

Manifest writes are atomic. Status values are `pending`, `running`, `completed`, and `failed`.

An analyst fingerprint includes:

- PDF content SHA-256.
- Effective versioned analyst instruction-contract SHA-256, covering the base
  production prompt, task wrapper and expected output, and relevant analyst
  role/goal/backstory.
- Score-masking setting.
- Strict-scoring setting.
- Resolved analyst model identifier.
- Effective analyst maximum-output-token setting.
- Checkpoint schema version.

The enabled analyst set is stored for observability but is not included in an individual analyst fingerprint. Enabling Delta later must not invalidate successful Alpha, Beta, and Gamma reports.

Runtime case-bundle content and transient retry diagnostics are excluded from the instruction-contract hash. PDF bytes remain a separate fingerprint input, and retry diagnostics are sanitized to stable Pydantic field paths and messages before they enter retry prompts or manifest history.

A checkpoint is reusable only when:

- Its status is `completed`.
- Its fingerprint matches the current analyst fingerprint.
- The referenced report exists.
- The shared validator accepts the report.

If any condition fails, that analyst is pending and will run. One analyst's incompatibility does not invalidate other compatible analysts.

Existing report files without a versioned manifest are treated as legacy checkpoints. They may be reused only when the shared validator accepts them and the existing `run_status.json`, when present, matches the PDF filename plus masking and strict-scoring settings. The legacy enabled-analyst list is not required to equal the current list, so enabling an additional analyst does not rerun existing default analysts. Once reused, reports are adopted into the new manifest with the current fingerprint. This is a one-time compatibility path for outputs created before manifests existed; `--force-rerun` bypasses it when the operator does not trust those files.

### 5. Batch Resume and Force Rerun

Batch iteration remains sorted by PDF filename.

For a normal invocation:

- Completed compatible PDFs are skipped and contribute rows to the rebuilt summary workbook.
- A failed or incomplete PDF enters `run_end_to_end`.
- The pipeline reuses each compatible validated analyst checkpoint.
- Execution begins with the first analyst that has no compatible validated checkpoint.

`--force-rerun` is propagated from `run_batch_folder` to `run_end_to_end` as `resume=False`. With resume disabled, no analyst report or manifest entry is reused. New validated results replace canonical reports atomically and refresh manifest entries.

Batch-level completion uses the same shared validator and compatibility rules as pipeline resume. A stale `completed` value in `run_status.json` is insufficient to skip a PDF.

The summary workbook is rebuilt from all PDFs encountered during the invocation, including skipped completed PDFs before the resumed failure point.

### 6. Error Reporting

Introduce a typed analyst execution error carrying:

- Analyst key.
- Attempts performed.
- Last error message.
- Whether the last failure occurred during execution or validation.

The batch wrapper includes the PDF filename when surfacing this error and writes the same information to `run_status.json`. The analyst manifest records attempt counts and the final analyst-local error.

Logs identify each attempt as `attempt 1/3`, `attempt 2/3`, or `attempt 3/3` without printing protected case content.

## Public Interface Changes

Add the following parameters:

```python
run_crew(..., max_restarts: int = 2)
run_end_to_end(..., max_restarts: int = 2, resume: bool = True)
run_batch_folder(..., max_restarts: int = 2)
```

`run_crew` strictly validates every supplied enabled `completed_reports` entry.
An invalid supplied entry is treated as incomplete and cannot bypass analyst
execution.

Add a batch and single-PDF CLI option:

```text
--analyst-restarts {0,1,2}
```

The default is `2`. Values outside `0..2` fail argument or parameter validation before ingestion starts.

`--force-rerun` sets `resume=False`; it does not alter the restart limit.

## Testing Strategy

### Validator tests

- Accept a complete Sections A, B, and strict fenced Section C report.
- Reject empty output, summary placeholders, missing Sections A/B/C, truncated JSON, missing score fields, incorrect totals, and inconsistent categories.
- Accept the legacy `alternative_causes_excluded` alias while normalizing to `other_causes_excluded`.
- Accept the documented missing injury-pattern and R-ratio values.

### Analyst retry tests

- Invalid twice and valid on the third attempt calls only that analyst three times and then continues.
- Invalid three times raises the typed analyst error and does not run later analysts.
- An execution exception followed by success consumes one restart.
- Each attempt receives a fresh crew/task instance.
- A successful prior analyst is retained when a later analyst fails.
- Canonical persistence occurs only after validation.

### Resume tests

- Rerunning a failed PDF skips compatible completed analysts and starts at the failed analyst.
- An invalid report file is not reused even if its manifest says completed.
- A prompt, model, masking, strict-scoring, PDF, or token-setting fingerprint change reruns only affected analysts.
- Enabling an additional analyst preserves compatible default-analyst checkpoints.
- A validated legacy report is adopted into the manifest.
- A partial manifest or partial report write is treated as pending rather than completed.

### Batch tests

- Completed PDFs before the failed PDF are skipped and still appear in the rebuilt workbook.
- Retry exhaustion marks the PDF failed and stops the batch.
- A later invocation completes the failed analyst and proceeds to remaining PDFs.
- `--force-rerun` bypasses all analyst checkpoints.
- CLI and Python parameter bounds reject more than two restarts.

### Regression tests

- Existing ingestion, masking, model routing, topology, and report-persistence tests continue to pass.
- The full test suite runs without live API calls.

## Documentation

Update `README.md` to explain:

- Two restarts means three total attempts.
- Retry is isolated to one analyst.
- A rerun resumes compatible analyst checkpoints.
- Which settings invalidate a checkpoint.
- `--force-rerun` bypasses analyst checkpoints.
- Where invalid attempt artifacts and checkpoint metadata are stored.

Update the active redesign execution plan to record this resilience extension and its verification status.

## Definition of Done

- An incomplete analyst report is detected before canonical persistence.
- The failing analyst is restarted no more than twice in one invocation.
- Successful analysts are not called again during a compatible resume.
- A failed PDF resumes at its first incomplete analyst on the next batch invocation.
- Exhausted retries preserve fail-fast batch behavior with analyst-specific diagnostics.
- `--force-rerun` performs a genuinely fresh analyst run.
- Checkpoint and report writes are atomic.
- Shared validation is used by retries, resume loading, batch completion, and summary extraction.
- Targeted and full offline test suites pass.
