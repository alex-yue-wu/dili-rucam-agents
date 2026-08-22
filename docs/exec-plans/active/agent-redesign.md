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
canonical report.

Normal single-PDF and batch reruns reuse only compatible validated analyst reports.
A batch skips a PDF only when its completion status and all enabled end-to-end
artifacts remain compatible; otherwise it resumes at the first incomplete analyst.
Legacy report directories without a manifest can be adopted only when their reports
validate and their existing batch context, when present, remains compatible.
`--force-rerun` disables all report and manifest reuse for a batch invocation,
including legacy-output adoption.

### Final verification

All checks were offline; no live model provider was invoked.

- `uv run ruff check src tests` — does not pass because of four pre-existing
  committed violations outside this documentation task: two unnecessary f-strings,
  one missing `Any` import, and one late test import.
- `uv run ruff format --check src tests` — does not pass because 13 pre-existing
  committed source and test files would be reformatted. No formatter changes were
  made outside the approved Task 6 files.
- `uv run pytest tests/test_analyst_report_validator.py tests/test_rucam_json_validator.py tests/test_checkpoints.py tests/test_crew_topology.py tests/test_batch.py tests/test_agents.py -q` — 103 passed.
- `uv run pytest -q` — 128 passed, with five pre-existing PyMuPDF/SWIG deprecation
  warnings from the ingestion smoke tests.
- `git diff --check`, `git status --short`, and `git diff --stat HEAD~5..HEAD` —
  run during the final scope inspection; no whitespace errors or generated files
  were found.
