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
- Deterministic resolution of prior-hepatotoxicity evidence for RUCAM item 6
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
     item 6 evidence cascade (deterministic, no LLM)
          ↓
     3 default analysts
          ↓
     up to 4 optional analysts
          ↓
     independent reports + JSON
```

### Repository Layout

```text
dilirank_v2/
docs/
src/dili_rucam_agents/
  crew/
  ingestion/
  hepatotoxicity/
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

## 5. Deterministic Item 6 Evidence Cascade

RUCAM item 6 (previous information on hepatotoxicity) is resolved between
masking and the analyst tier, never by an analyst from memory.

Sources are consulted in a fixed order and the cascade stops at the first one
that resolves the drug:

1. `dilirank_v2/DILIrank_v2.xlsx` (FDA DILIrank v2), offline and pinned.
   vMost- and vLess-DILI-concern score `+2`, Ambiguous-DILI-concern `+1`,
   vNo-DILI-concern `0`.
2. The current FDA label on DailyMed. Liver injury in a boxed warning or a
   warnings-and-precautions section scores `+2`, in adverse reactions `+1`.
3. PubMed case reports, with a fixed query bounded by an as-of publication date.
   One or more reports score `+1`, none scores `0`.

Responsibilities:

- Resolve every DILIrank compound name occurring in the case bundle text, and
  any drug name supplied explicitly by the caller.
- Match names through one deterministic normaliser covering salt, hydrate and
  formulation suffixes, British spellings, INN/USAN aliases and salt-first word
  order, trying the exact name before any variant. The networked tiers search
  those variants in order and record which one matched.
- Attach the result to the case bundle as `hepatotoxicity_evidence`, with the
  deciding tier, the score and the provenance behind it.
- Persist a markdown artifact for the resolution when an output directory is
  supplied.
- Cache every networked answer on disk so a score never depends on the day the
  pipeline ran.

The analyst prompt reads item 6 off this block (Appendix C) and may not move a
resolved score. Where the cascade resolved nothing, the prompt falls back to
rungs that require a labelling quote or a citation in the case text.

## 6. RUCAM Analyst Agents

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
- `ANALYST_DELTA_MODEL`: `deepseek-v4-pro`
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
- default when none is set: 32000; a restart after a truncated report doubles the
  budget up to a ceiling of 64000

Each analyst must:

1. Extract clinical facts strictly from `case_bundle_json`.
2. Compute the R-ratio.
3. Determine injury pattern.
4. Apply the correct RUCAM scoring table.
5. Score all 7 RUCAM items.
6. Produce Sections A, B, and C exactly as specified by the production prompt.

## 7. Required Output Format

### SECTION A

Human-readable report with case summary, timeline, injury pattern, and scoring rationale.

### SECTION B

RUCAM scoring table.

### SECTION C

Strict JSON with injury pattern, R-ratio, itemized scores, total score, and category.

## 8. Engineering Standards

- Temperature remains 0 for all configured models when supported; models that reject explicit temperature use the provider-required default.
- The analyst output budget must leave room for the whole report on a reasoning
  model, where it also covers hidden reasoning tokens. A cut-off report is
  reported as a truncation, not as a missing section, and the restart that
  follows it runs with a wider budget.
- Ingestion, masking and the item 6 evidence cascade must be testable without
  live API calls.
- The DILIrank tier stays offline. The DailyMed and PubMed tiers are consulted
  only for explicitly supplied drug names, are cached on disk, and are bounded by
  an as-of date.
- Optional agents are activated only by explicit configuration.
- Persist one report per enabled analyst when an output directory is supplied.
- When masking is enabled, persist a markdown artifact for the masked case bundle with a stable section layout.

## 9. Definition Of Done

A run is valid if:

- `case_bundle_json` is generated successfully
- the optional masking step preserves schema and redacts targeted RUCAM content when enabled
- `hepatotoxicity_evidence` resolves every drug it can and records the deciding
  source for each score
- the 3 default analysts complete Sections A, B, and C
- any enabled optional analysts complete Sections A, B, and C
- final JSON validates against schema
- no invented data appears in outputs
