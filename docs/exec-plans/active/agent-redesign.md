# Agent Redesign Plan

## Goal

Redesign the pipeline to remove arbiter agents, add an optional score-masking agent between ingestion and the analyst tier, and make the analyst tier scale from 3 default analysts to 7 total analysts.

## Scope

- Remove arbiter agents, tasks, prompts, CLI flags, and persisted arbiter reports.
- Add an optional masking agent that consumes the canonical extracted bundle and redacts prior RUCAM scores and score-derived conclusions before analysts run.
- Make three RUCAM analyst agents the default.
- Allow four additional RUCAM analyst agents to be enabled optionally.
- Update tests and docs to reflect the new architecture.

## Current State

- The crew is hard-coded to two analysts and one-to-three arbiters.
- The pipeline persists analyst and arbiter markdown outputs with legacy filenames.
- Tests cover model routing but not the new configurable analyst topology.

## Proposed Design

1. Keep deterministic ingestion as the first task and canonical data source.
2. Insert an optional masking task after ingestion. When enabled, analysts consume the masked bundle instead of the raw bundle.
3. Build analyst agents from a shared configuration list:
   - Three enabled by default.
   - Four disabled by default and activated by explicit flags.
4. Remove all arbiter construction and arbitration tasks.
5. Persist one markdown file per analyst report when `--output-dir` is supplied.

## Implementation Steps

1. Add the documentation scaffold and this execution plan.
2. Implement masking utilities, masking agent/task, and configurable analyst definitions.
3. Refactor crew and pipeline wiring for 3 default plus 4 optional analysts.
4. Update README and spec docs.
5. Add tests for masking, crew topology, and report persistence.
6. Run targeted tests and fix regressions.

## Risks

- Masking rules can over-redact clinically relevant evidence if they are too broad.
- Existing consumers may still rely on legacy analyst keys or arbiter report filenames.
- CrewAI task context needs to stay valid when swapping the analyst input task from raw bundle to masked bundle.

## Review Checklist

- No arbiter code path remains reachable from the public pipeline.
- Default run produces three analyst reports.
- Optional analyst flags add four more analysts without changing the default contract.
- Masking is optional and only affects downstream analyst inputs.
- Tests cover the new topology and report filenames.

## Completed Resilience Extension: Analyst Retry and Resume

The analyst workflow now uses one shared complete-report validator as the success
boundary for retry handling, checkpoint reuse, batch completion, and summary
extraction. Every analyst has one initial attempt and up to two restarts per
invocation (three attempts total by default); retries are isolated to the failing
analyst, remain sequential, and preserve fail-fast behavior after exhaustion.

Once analyst execution starts, its PDF output directory contains a versioned
`analyst_checkpoints.json` manifest. It tracks each analyst independently,
including compatible fingerprints, attempt counts, and failure diagnostics.
Canonical analyst reports, manifests, and invalid-attempt artifacts are persisted
atomically; invalid report text is retained under `attempts/` and never replaces a
canonical report. Invalid-attempt names use a cumulative attempt number plus a
unique identifier, and manifest history retains every artifact path across
invocations.

Checkpoint compatibility uses a versioned effective analyst-instruction snapshot
shared by fingerprint generation and actual agent/task construction. The snapshot
includes the production prompt, static task and retry wrappers, expected output,
and analyst role/goal/backstory; runtime case-bundle content and transient retry
diagnostics are excluded. Validation diagnostics are converted into a validated
structure containing only allowlisted field paths and issue codes. Retry prompts,
public events, terminal errors, manifests, batch status/logs, and workbook cells
render fixed messages only from that structure; raw invalid report text is confined
to its audit artifact.
The public `run_crew` boundary also validates supplied completed reports before
allowing them to skip execution. Existing-manifest writes refresh the enabled
analyst topology metadata during normal resume, while read-only completion checks
never mutate it.

Normal single-PDF and batch reruns reuse only compatible validated analyst reports.
A batch skips a PDF only when its completion status and all enabled end-to-end
artifacts remain compatible; a completed expanded topology can contract without
rerunning or mutating its manifest, while expansion or incompatibility resumes at
the first incomplete analyst. Strict validation requires exactly one SECTION A,
SECTION B, and SECTION C in order and exactly one fenced JSON object in SECTION C;
legacy summary parsing deliberately retains last-fenced-or-unfenced-JSON recovery,
but checkpoint adoption and public `completed_reports` remain strict at the complete
canonical report boundary. Execution diagnostics
persist only bounded exception types and safe numeric identifiers, not raw provider
messages, credentials, request fragments, or patient text. Public analyst-attempt
events carry a validated structured diagnostic instead of the raw exception and are
safe under ordinary dataclass and pickle serialization; the original exception stays
local for terminal chaining. Persistence independently validates and renders the
structured diagnostic and rejects invalid diagnostics or unknown failure kinds before
writing. Successful completion clears stale current failure fields while preserving
attempt history.
Legacy report directories without a manifest can be adopted only when their reports
pass strict canonical validation and their existing batch context, when present,
remains compatible. A manifestless completed batch enters normal resume once to
create the versioned manifest; only a compatible versioned manifest may satisfy the
read-only batch completion gate.
Schema v3 is the first manifest version covered by the structured validation-
diagnostic boundary. Pre-v3 manifests are rejected rather than migrated because
schema v2 allowed arbitrary validation-error strings in current and historical
fields. Read-only completion returns incomplete without mutation; normal execution
reruns analysts and creates a clean v3 manifest with no inherited error text or
attempt history.
`--force-rerun` disables all report and manifest reuse for a batch invocation,
including legacy-output adoption.

### Final verification

All checks were offline; no live model provider was invoked.

- `uv run ruff check <feature-changed Python files>` and `uv run ruff check src
  tests` — both report the same three baseline findings already present at the
  feature starting commit `0a1dad6`: F541 in `batch.py`, F821 in `agents.py`, and
  F541 in `tasks.py`. The feature-introduced E402 was removed.
- `uv run ruff format --check <Task 8 touched Python files>` — all 10 files are
  formatted. No unrelated Python files were reformatted.
- `uv run pytest tests/test_analyst_report_validator.py tests/test_rucam_json_validator.py tests/test_checkpoints.py tests/test_crew_topology.py tests/test_batch.py tests/test_agents.py -q` — 157 passed.
- `uv run pytest -q` — 182 passed, with five pre-existing PyMuPDF/SWIG deprecation
  warnings from the ingestion smoke tests.
- `git diff --check`, `git status --short`, and `git diff --stat 0a1dad6..HEAD` —
  run during the final scope inspection; no whitespace errors or generated files
  were found.
