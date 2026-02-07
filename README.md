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
 │ Arbiter(s) (Expert)       │
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
export GEMINI_API_KEY=...
export DEEPSEEK_API_KEY=...
export OPENROUTER_API_KEY=...
export ANTHROPIC_API_KEY=...
export ARBITER_ALPHA_MODEL=...
export ARBITER_BETA_MODEL=...
export ARBITER_GAMMA_MODEL=...

# default dual-analyst + Arbiter Alpha flow
uv run python -m dili_rucam_agents.pipeline examples/3568943.pdf

# persist analyst + arbiter markdown and enable the optional Beta/Gamma arbiters
uv run python -m dili_rucam_agents.pipeline \
    examples/3568943.pdf \
    --output-dir examples/latest_reports \
    --arbiter-beta \
    --arbiter-gamma
```

- `--arbiter-beta` turns on a second arbiter (defaults to GPT-5.2 unless `ARBITER_BETA_MODEL` is set, recommend Kimi-K2).
- `--arbiter-gamma` turns on a third arbiter (defaults to GPT-5.2 unless `ARBITER_GAMMA_MODEL` is set, recommend Anthropic Claude Sonnet 4.5).
- The base flow always runs GPT-5.2 + Gemini 3.0 analysts and Arbiter Alpha; additional arbiters let you compare multiple rulings for sensitive cases.

When `--output-dir` is supplied, the pipeline stores:

- `gpt-5.2_report.md` and `gemini-3.0_report.md` — analyst-facing Section A/B/C reports.
- `arbiter-arbiter-alpha_report.md` (and `arbiter-arbiter-beta_report.md`, `arbiter-arbiter-gamma_report.md` when enabled) — arbiter outputs containing Sections A–D.

Use these markdown files for regression review or to diff arbitrations across model configurations.

## Arbiter Ensemble + Section D

- **Arbiter Alpha** (DeepSeek Reasoner by default) runs every time and resolves GPT vs Gemini disagreements.
- **Optional Arbiters** provide additional hepatology opinions:
  - `--arbiter-beta` defaults to GPT-5.2 but can be retargeted.
  - `--arbiter-gamma` defaults to GPT-5.2 but can be retargeted.
- Every arbiter now emits a **SECTION D — Arbiter Justification**, a discrepancy table explaining why each RUCAM item score was accepted or rejected. Sections A–C mirror the analyst format so downstream automation can parse a consistent structure.

## Configuration

Environment variables let you pin each model deterministically:

| Variable                         | Purpose                                                                                         | Default Fallback       |
| -------------------------------- | ----------------------------------------------------------------------------------------------- | ---------------------- |
| `OPENAI_API_KEY`, `OPENAI_MODEL` | GPT-5.2 analyst + general fallback                                                              | `gpt-5.2`              |
| `GEMINI_API_KEY`, `GEMINI_MODEL` | Gemini 3.0 analyst                                                                              | `gemini-3-pro-preview` |
| `INGESTION_MODEL`                | Override the deterministic ingestion helper (defaults to `OPENAI_MODEL` or `gpt-4o-mini`).      |
| `ARBITER_ALPHA_MODEL`            | Primary arbiter (DeepSeek by default). Falls back to `ARBITER_MODEL` → `OPENAI_MODEL`.          |
| `ARBITER_BETA_MODEL`             | Secondary arbiter when `--arbiter-beta` is set. Falls back to `ARBITER_MODEL` → `OPENAI_MODEL`. |
| `ARBITER_GAMMA_MODEL`            | Tertiary arbiter when `--arbiter-gamma` is set. Falls back to `ARBITER_MODEL`                   |
| `ARBITER_MODEL`                  | Shared fallback for any arbiter without its own override.                                       |

Set only the variables you need; the factories automatically select the correct API base (OpenAI, Anthropic, DeepSeek, or OpenRouter) and temperature=0 for determinism.

## Test The Ingestion Tools

Run the focused ingestion test suite before wiring the LLM agents so you can confirm deterministic PDF parsing works:

```bash
uv run pytest tests/test_ingestion.py
```

Pytest ships with the project dependencies (see `pyproject.toml`), so `uv sync` installs it automatically. This suite exercises `build_case_bundle` and the `CaseBundleExtractionTool`, ensuring missing-PDF errors surface early and that the tool emits well-formed `case_bundle_json`. The ingestion stack automatically falls back to the unstructured `fast` strategy if Poppler/pdf2image (required for `hi_res`) is not installed, so these tests still pass on lightweight environments. Execute them whenever you touch the ingestion layer or before delivering bundles to the analyst agents.

To cover the latest arbiter model-routing logic and schema validation, also run:

```bash
uv run pytest tests/test_agents.py tests/test_bundle_schema.py tests/test_rucam_json_validator.py
```

`tests/test_agents.py` ensures we send the correct `base_url` / `custom_llm_provider` for DeepSeek, OpenRouter, and Anthropic arbiters, guarding against silent routing regressions when you add new models.
