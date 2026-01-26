# DILI RUCAM Agents

Production-grade CrewAI implementation for dual-model RUCAM causality assessment of drug-induced liver injury (DILI) case report PDFs.

## Purpose

This agent system performs **production-grade RUCAM causality assessment** from clinical
case report PDFs. It is designed for **published research PDFs** with variable layout
(1–2 columns, sometimes 3 in abstracts), tables, and figures.

The system guarantees:

- Deterministic, auditable PDF ingestion
- Independent dual-model RUCAM scoring
- Expert arbitration of disagreements
- Strict structured JSON outputs suitable for automation

This agent is **decision-support only**, not a medical diagnosis tool.

## High-Level Architecture

```
PDF
 └─ Ingestion (deterministic, no LLM)
     ├─ unstructured (layout-aware blocks)
     ├─ pdfplumber (tables)
     └─ PyMuPDF fallback (adaptive 1/2/3-column)
          ↓
     case_bundle (canonical JSON)
          ↓
 ┌───────────────────────────┐
 │ GPT-5.2 RUCAM Analyst     │
 └───────────────────────────┘
          ↓
 ┌───────────────────────────┐
 │ Gemini 3.0 RUCAM Analyst  │
 └───────────────────────────┘
          ↓
 ┌───────────────────────────┐
 │ Arbiter (Expert)          │
 └───────────────────────────┘
          ↓
 Final RUCAM report + JSON
```

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
# save individual analyst/arbiter reports as markdown (gpt-5.2, gemini-3.0, arbiter)
uv run python -m dili_rucam_agents.pipeline examples/3568943.pdf --output-dir examples/latest_reports
```

When `--output-dir` is supplied, the pipeline stores `gpt-5.2_report.md`, `gemini-3.0_report.md`, and `arbiter_report.md` inside the target folder after each run, making it easy to inspect or diff the analyst outputs.

## Test The Ingestion Tools

Run the focused ingestion test suite before wiring the LLM agents so you can confirm deterministic PDF parsing works:

```bash
uv run pytest tests/test_ingestion.py
```

Pytest ships with the project dependencies (see `pyproject.toml`), so `uv sync` installs it automatically. This suite exercises `build_case_bundle` and the `CaseBundleExtractionTool`, ensuring missing-PDF errors surface early and that the tool emits well-formed `case_bundle_json`. The ingestion stack automatically falls back to the unstructured `fast` strategy if Poppler/pdf2image (required for `hi_res`) is not installed, so these tests still pass on lightweight environments. Execute them whenever you touch the ingestion layer or before delivering bundles to the analyst agents.
