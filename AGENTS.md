# AGENTS.md

Current redesign work is tracked in [docs/exec-plans/active/agent-redesign.md](/Users/alexwu/Documents/GitHub/dili-rucam-agents/docs/exec-plans/active/agent-redesign.md).

# Production Agent Specification

**Project:** Multi-Analyst RUCAM Causality Assessment  
**Default topology:** one analyst, declared in `config/analysts.json`  
**Optional topology:** any analyst roster file, selected with `--analysts <path>`  
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
     analysts declared in the roster file (default: one)
          ↓
     independent reports + JSON
```

### Repository Layout

```text
config/
dilirank_v2/
docs/
src/dili_rucam_agents/
  ingestion/
  hepatotoxicity/
  masking.py
  pipeline.py
  prompts/
  validators/
  roster.py          who scores, on which vendor, and with what budget
  providers.py       one vendor SDK call each; the CompletionProvider protocol
  runner.py          render, call, parse, validate, retry; the four budgets
  contracts.py       the instruction contract an analyst is sent, and its hash
  analysts.py        run every roster analyst over one case bundle
  rendering.py       Sections A, B and C, written from the returned object
  ground_truth.py    the published RUCAM score, read in one free-text call
  checkpoints.py     the resume manifest, its fingerprints and its counters
  verify.py          the roster pre-flight: one real call per declared model
  diagnostics.py     the safe-diagnostic boundary: no provider text escapes
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

### Declared roster

Analysts come from a roster file, never from environment variables and never
from a fixed tuple in code. `--analysts <path>` selects a roster; omitting it
uses `config/analysts.json`. Three roster files ship:

- `config/analysts.json` - **one** analyst. This is the default, so a caller who
  passes no roster gets one analyst.
- `config/analysts.study.json` - seven analysts, the condition the measured
  retry baseline was taken in.
- `config/analysts.smoke.json` - five analysts, one per provider adapter, for the
  cheapest run that touches every transport.

Each entry declares a `key`, a `label`, a `provider`, a `model`, a
`max_output_tokens` budget (default 32000) and an optional `api_version`. The
provider is declared explicitly and is one of `openai`, `anthropic`, `google`,
`openrouter` or `deepseek`; it is never inferred from the model string. The
model name is passed to the vendor SDK as written.

`api_version` names the API surface the model is served on. Only the google
client accepts one, so declaring it on any other provider is refused rather than
ignored; unset, the vendor SDK's own default applies. It is not cosmetic: a
model can be absent from one API version and served on another, so the study
and smoke rosters both declare `api_version: "v1alpha"` on the google entry
because `gemini-3.1-pro-preview` is `404 not found for API version v1` on the
google SDK's default. Measured 2026-11-11, and again by the roster pre-flight
on 2026-11-12 under a rotated key: the same 404 both times, so it belongs to
the model rather than to a key. Clients are cached per `(provider, api_version)`, so two
google analysts on different surfaces cannot be served one another's client.
The surface belongs in the roster rather than the adapter because it is part of
the condition a score was measured in. API keys come from the environment
(`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`,
`DEEPSEEK_API_KEY`); the CLIs read a `.env` file from the directory the command
was run in, and values already exported win.
Before ingestion or any model call, every provider the run will use (the
roster's analysts, plus the ground-truth provider when masking is on) is key-
checked, and a failure names every missing variable at once.

### Per-analyst contract

The model returns one JSON object; Python renders Sections A, B and C. The
model supplies neither `total_score` nor `category`: code computes both from
the seven item scores.

**Item 5's score is derived too**, from the Group I / Group II checklist the
same reply carries, because steps 2-5 of its printed procedure are arithmetic
over that checklist and nothing else: `g1 = 6` with both Group II categories
ruled out is +2, `g1 = 6` alone is +1, 5 or 4 is 0, and 3 or fewer is -2. The
judgement stays the model's -- it decides each cause's status. Step 1's -3 rung
needs a cause to be *a highly probable explanation* of the injury, which the
eight statuses cannot express, so a claimed -3 stands wherever some cause is
documented `present` and falls through to the arithmetic when none is. A
derived score that differs from the stated one is printed beside it in Section
A, the way an R-ratio disagreement is; Sections B and C carry the derived one,
since Section C's items must sum to its total.

Each analyst must:

1. Extract clinical facts strictly from `case_bundle_json`.
2. Compute the R-ratio.
3. Determine injury pattern.
4. Apply the correct RUCAM scoring table.
5. Score all 7 RUCAM items.
6. Return the assessment as a single JSON object.

Each analyst writes two artifacts, `<key>_report.md` and
`<key>_assessment.json` (the key with `_` written as `-`). A checkpoint counts
complete only when both are present and valid. `CHECKPOINT_SCHEMA_VERSION` is 4;
a version-3 manifest is discarded rather than migrated, so every manifest
written before that version is invalidated.

### Retry budgets

Four budgets are separate and are never pooled:

- validation retries, for a reply that parsed but broke the schema (default 2);
- transient transport retries, for provider errors worth retrying (4, with
  backoff of 0.5/1/2/4 seconds plus jitter, reset by any successful call);
- one truncation escalation, which widens the output budget up to a ceiling of
  64000;
- structured-output mode downgrades along a `json_schema` -> `json_object` ->
  `text` ladder that starts at the highest mode each provider actually supports
  (`deepseek` starts at `json_object`).

The manifest records `validation_retries`, `transient_retries`,
`truncation_escalations` and `mode_downgrades` per analyst, plus the final mode.
The four counts are **cumulative across invocations**: a resumed run adds its
spend to what the earlier invocation recorded, so a retry an analyst spent before
a failure is not erased by the resume that the failure message recommends. Each
failed invocation's own block is also kept in that analyst's `attempt_history`.

## 7. Required Output Format

### SECTION A

Human-readable report with case summary, timeline, injury pattern, and scoring rationale, rendered by Python from the model's JSON object.

### SECTION B

RUCAM scoring table.

### SECTION C

Strict JSON with injury pattern, R-ratio, itemized scores, total score, and category. `total_score` and `category` are computed by code from the seven items, not supplied by the model, and item 5's score is itself derived from the Group I / Group II checklist.

## 8. Engineering Standards

- Temperature remains 0 for all configured models when supported; models that reject explicit temperature use the provider-required default.
- The analyst output budget (`max_output_tokens` in the roster) must leave room
  for the whole report on a reasoning model, where it also covers hidden
  reasoning tokens. A cut-off reply is reported as a truncation, not as a missing
  section, and the one escalation that follows it runs with a wider budget.
- `rules_version` is `RUCAM_DananBenichou1993_modified_sheet_v3`, and the frozen
  rules core in both prompts is unchanged by the roster refactor.
- Latency from a dose of the suspect drug is numbered from 1: the calendar day of
  the dose is Day 1, so an onset on the day of the dose lands on a printed rung
  of item 1 instead of falling off the sheet as day 0. Elapsed-day durations that
  are not anchored on a dose -- item 2's clock from the peak, item 4's lookback
  window -- stay elapsed and are not renumbered.
- Ingestion, masking and the item 6 evidence cascade must be testable without
  live API calls.
- The DILIrank tier stays offline. The DailyMed and PubMed tiers are consulted
  only for explicitly supplied drug names, are cached on disk, and are bounded by
  an as-of date.
- The analyst set is whatever the declared roster lists; a larger roster is activated only by passing `--analysts`.
- Persist `<key>_report.md` and `<key>_assessment.json` per analyst when an output directory is supplied.
- When masking is enabled, persist a markdown artifact for the masked case bundle with a stable section layout.

## 9. Definition Of Done

A run is valid if:

- `case_bundle_json` is generated successfully
- the optional masking step preserves schema and redacts targeted RUCAM content when enabled
- `hepatotoxicity_evidence` resolves every drug it can and records the deciding
  source for each score
- every analyst in the declared roster completes Sections A, B, and C and has a
  valid `<key>_report.md` and `<key>_assessment.json`; a one-analyst roster is a
  valid run
- final JSON validates against schema
- no invented data appears in outputs
