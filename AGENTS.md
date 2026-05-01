# AGENTS.md

Current redesign work is tracked in [docs/exec-plans/active/agent-redesign.md](/Users/alexwu/Documents/GitHub/dili-rucam-agents/docs/exec-plans/active/agent-redesign.md).

# Production Agent Specification

**Project:** Multi-Analyst RUCAM Causality Assessment  
**Default topology:** 3 analyst agents  
**Optional topology:** +1 score masking agent, +4 analyst agents  
**Domain:** Drug-Induced Liver Injury (DILI), RUCAM  
**Input:** Clinical case report PDF (2–5 pages, 1–3 columns, tables, figures)  
**Python Package Manager:** uv

## 1. Purpose

This system performs production-grade RUCAM causality assessment from published clinical case report PDFs.

It guarantees:

- Deterministic, auditable PDF ingestion
- Optional deterministic masking of prior RUCAM scores and causality conclusions found in source text
- Independent multi-analyst RUCAM scoring
- Strict structured JSON outputs suitable for automation

## 2. Software Architecture

```text
PDF
 └─ Ingestion (deterministic, no LLM)
     ├─ unstructured
     ├─ pdfplumber
     └─ PyMuPDF fallback
          ↓
     case_bundle (canonical JSON)
          ↓
     optional score masking
          ↓
     3 default analysts
          ↓
     up to 4 optional analysts
          ↓
     independent reports + JSON
```

### Repository Layout

```text
docs/
src/dili_rucam_agents/
  crew/
  ingestion/
  masking.py
  pipeline.py
  prompts/
  validators/
tests/
README.md
AGENTS.md
agent.md
```

## 3. Deterministic Ingestion Layer

All downstream agents must consume the same canonical `case_bundle_json`.

Rules:

- No LLM may invent information not present in the case bundle.
- Missing information must be labeled `Not reported`.
- Ingestion remains deterministic and auditable.

## 4. Optional Score Masking Step

The score masking step runs between ingestion and the analyst tier when enabled.

Responsibilities:

- Detect explicit RUCAM score mentions in extracted text
- Detect score tables and explicit causality-category statements tied to RUCAM
- Replace those references with a stable masking token
- Preserve the original case bundle schema and non-RUCAM clinical evidence

This step is heuristic and deterministic. It is intended to reduce leakage from source documents that already contain RUCAM judgments, and the same masked bundle is used both for analyst input and for persisted masked-output reporting.

## 5. RUCAM Analyst Agents

### Default analysts

- Analyst Alpha
- Analyst Beta
- Analyst Gamma

### Optional analysts

- Analyst Delta
- Analyst Epsilon
- Analyst Zeta
- Analyst Eta

### Default model lineup

- `ANALYST_ALPHA_MODEL`: `gpt-5.5`
- `ANALYST_BETA_MODEL`: `gemini-3.1-pro-preview`
- `ANALYST_GAMMA_MODEL`: `moonshotai/kimi-k2.5`
- `ANALYST_DELTA_MODEL`: `deepseek-reasoner`
- `ANALYST_EPSILON_MODEL`: `qwen/qwen3.5-plus-02-15`
- `ANALYST_ZETA_MODEL`: `claude-opus-4-7`
- `ANALYST_ETA_MODEL`: `z-ai/glm-5`

### Analyst max-token controls

- `ANALYST_ALPHA_MAX_TOKENS`
- `ANALYST_BETA_MAX_TOKENS`
- `ANALYST_GAMMA_MAX_TOKENS`
- `ANALYST_DELTA_MAX_TOKENS`
- `ANALYST_EPSILON_MAX_TOKENS`
- `ANALYST_ZETA_MAX_TOKENS`
- `ANALYST_ETA_MAX_TOKENS`
- shared fallback: `ANALYST_MAX_TOKENS`
- global fallback: `LLM_MAX_TOKENS`

Each analyst must:

1. Extract clinical facts strictly from `case_bundle_json`.
2. Compute the R-ratio.
3. Determine injury pattern.
4. Apply the correct RUCAM scoring table.
5. Score all 7 RUCAM items.
6. Produce Sections A, B, and C exactly as specified by the production prompt.

## 6. Required Output Format

### SECTION A

Human-readable report with case summary, timeline, injury pattern, and scoring rationale.

### SECTION B

RUCAM scoring table.

### SECTION C

Strict JSON with injury pattern, R-ratio, itemized scores, total score, and category.

## 7. Engineering Standards

- Temperature remains 0 for all configured models.
- Ingestion and masking must be testable without live API calls.
- Optional agents are activated only by explicit configuration.
- Persist one report per enabled analyst when an output directory is supplied.
- When masking is enabled, persist a markdown artifact for the masked case bundle with a stable section layout.

## 8. Definition Of Done

A run is valid if:

- `case_bundle_json` is generated successfully
- the optional masking step preserves schema and redacts targeted RUCAM content when enabled
- the 3 default analysts complete Sections A, B, and C
- any enabled optional analysts complete Sections A, B, and C
- final JSON validates against schema
- no invented data appears in outputs
