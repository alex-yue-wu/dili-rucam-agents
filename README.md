# dili-rucam-agents

Production-grade CrewAI implementation for dual-model RUCAM causality assessment of drug-induced liver injury (DILI) case report PDFs.

## Repository Layout

- `agent.md` — canonical specification.
- `src/dili_rucam_agents/ingestion/` — deterministic PDF ingestion stack (unstructured, pdfplumber, PyMuPDF) producing `case_bundle_json`.
- `src/dili_rucam_agents/prompts/` — production RUCAM prompt.
- `src/dili_rucam_agents/crew/` — CrewAI agents, tasks, and crew builder.
- `src/dili_rucam_agents/pipeline.py` — CLI/SDK entry point (`run_end_to_end`).
- `src/dili_rucam_agents/validators/` — schema enforcement for SECTION C JSON.
- `tests/` — fixtures and smoke tests for ingestion and validation.

## Quickstart

```bash
uv sync
export OPENAI_API_KEY=...
export GOOGLE_API_KEY=...
uv run python -m dili_rucam_agents.pipeline examples/3568943.pdf
```

## Test The Ingestion Tools

Run the focused ingestion test suite before wiring the LLM agents so you can confirm deterministic PDF parsing works:

```bash
uv run pytest tests/test_ingestion.py
```

Pytest ships with the project dependencies (see `pyproject.toml`), so `uv sync` installs it automatically. This suite exercises `build_case_bundle` and the `CaseBundleExtractionTool`, ensuring missing-PDF errors surface early and that the tool emits well-formed `case_bundle_json`. The ingestion stack automatically falls back to the unstructured `fast` strategy if Poppler/pdf2image (required for `hi_res`) is not installed, so these tests still pass on lightweight environments. Execute them whenever you touch the ingestion layer or before delivering bundles to the analyst agents.
