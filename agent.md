# agent.md — Production Agent Specification (Codex)

**Project:** Dual-Model RUCAM Causality Assessment  
**Models:** GPT-5.2 (OpenAI), Gemini 3.0 (Google)  
**Domain:** Drug-Induced Liver Injury (DILI), RUCAM  
**Input:** Clinical case report PDF (2–5 pages, 1–3 columns, tables, figures)
**Python Package Manager:** uv

---

## 1. Purpose

This agent system performs **production-grade RUCAM causality assessment** from clinical
case report PDFs. It is designed for **published research PDFs** with variable layout
(1–2 columns, sometimes 3 in abstracts), tables, and figures.

The system guarantees:

- Deterministic, auditable PDF ingestion
- Independent dual-model RUCAM scoring
- Expert arbitration of disagreements
- Strict structured JSON outputs suitable for automation

This agent is **decision-support only**, not a medical diagnosis tool.

---

## 2. Software Architecture

### 2.1 High-Level Architecture

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

### 2.2 Repository Layout (recommended)

```
├─ src/
│  ├─ ingestion/
│  │  ├─ case_bundle.py            # dataclasses + schema helpers
│  │  ├─ unstructured_ingest.py    # partition_pdf wrapper
│  │  ├─ pdfplumber_tables.py      # table extraction + cleaning
│  │  ├─ pymupdf_fallback.py       # adaptive 1/2/3-col extractor
│  │  └─ build_bundle.py           # orchestration + quality scoring
│  ├─ prompts/
│  │  └─ rucam_analysis_production.md       # the full production prompt
│  ├─ crew/
│  │  ├─ agents.py                 # Agent definitions (GPT, Gemini, Arbiter)
│  │  ├─ tasks.py                  # Task definitions referencing prompt
│  │  └─ crew.py                   # build_crew()
│  ├─ pipeline.py                  # run_end_to_end(pdf_path)
│  └─ validators/
│     └─ rucam_json.py             # strict JSON schema validation
├─ tests/
│  ├─ fixtures/
│  │  └─ example_case.pdf
│  ├─ test_ingestion.py
│  ├─ test_bundle_schema.py
│  └─ test_rucam_json_validator.py
├─ .env
├─ README.md
└─ agent.md
```

---

## 3. Deterministic Ingestion Layer (MANDATORY)

### 3.1 Tools Used

- **unstructured**
  - Extracts layout-aware elements (titles, narrative text, captions, table text)
- **pdfplumber**
  - Extracts tables into row/column arrays (labs, medications)
- **PyMuPDF (fitz) — fallback**
  - Adaptive 1/2/3-column extraction when layout parsing is poor

### 3.2 Why deterministic ingestion is required

RUCAM scoring depends on:

- exact lab values + ULNs
- precise timing (start, onset, stop, resolution)
- negative workup documentation
- course after drug cessation

Retrieval-only PDF tools are insufficient for multi-column research PDFs.
All LLM agents must receive the **same canonical input**.

---

## 4. Canonical Input Contract: `case_bundle`

All LLM agents MUST consume a single JSON object named `case_bundle_json`.

### 4.1 Schema (stable)

```json
{
  "pdf_path": "string",
  "extraction_notes": ["string"],
  "blocks": [
    {
      "element_type": "Title | NarrativeText | Table | Caption | ...",
      "page_number": 1,
      "text": "string"
    }
  ],
  "normalized_text": "string",
  "tables": [
    {
      "page_number": 2,
      "table_index": 1,
      "raw_rows": [["cell", "cell"]],
      "preview": "string"
    }
  ],
  "unknowns": ["string"],
  "quality": {
    "unstructured_total_score": 0,
    "fallback_pages": [],
    "fallback_total_score": 0
  }
}
```

### 4.2 Rules

- No LLM may invent information not present in `case_bundle`
- Missing information must be explicitly labeled **“Not reported”**
- All models receive **identical** `case_bundle_json`

---

## 5. RUCAM Analyst Agents (GPT-5.2 and Gemini 3.0)

### 5.1 Role

**Expert DILI Causality Analyst (RUCAM Specialist)**

### 5.2 Responsibilities

Each analyst must:

1. Extract clinical facts strictly from `case_bundle`
2. Compute **R-ratio**:
   ```
   R = (ALT / ULN_ALT) / (ALP / ULN_ALP)
   ```
3. Determine injury pattern:
   - R ≥ 5 → Hepatocellular
   - 2 < R < 5 → Mixed
   - R ≤ 2 → Cholestatic
4. Apply the correct **RUCAM scoring table**
5. Score all **7 RUCAM items**
6. Produce structured outputs (Sections A/B/C)

### 5.3 Prompt discipline

- Use the **production RUCAM prompt** verbatim
- Temperature = 0
- No creative interpretation
- Conservative scoring when evidence is ambiguous

---

## 6. Arbiter Agent

### 6.1 Role

**Senior Hepatology Arbiter**

### 6.2 Responsibilities

The arbiter:

- Compares GPT-5.2 vs Gemini analyses
- Identifies disagreements in:
  - extracted facts
  - R-ratio
  - injury pattern
  - each RUCAM item score
- Selects the interpretation most faithful to:
  - evidence in `case_bundle`
  - published RUCAM rules
- Produces a **single authoritative final decision**

### 6.3 Arbiter output

The arbiter MUST output:

- **SECTION A:** Disagreement analysis and rationale
- **SECTION B:** Final consolidated RUCAM table
- **SECTION C:** Final JSON (canonical)

---

## 7. Required Output Format (ALL LLM AGENTS)

### SECTION A — Human-Readable Report

- Case summary
- Timeline
- Injury pattern determination
- Explanation of scoring decisions

### SECTION B — RUCAM Scoring Table

| RUCAM Item | Score | Evidence |
| ---------- | ----- | -------- |

### SECTION C — JSON (STRICT)

```json
{
  "injury_pattern": "hepatocellular | mixed | cholestatic",
  "R_ratio": 0.0,
  "rucam_scores": {
    "time_to_onset": 0,
    "course": 0,
    "risk_factors": 0,
    "concomitant_drugs": 0,
    "alternative_causes_excluded": 0,
    "known_hepatotoxicity": 0,
    "rechallenge": 0
  },
  "total_score": 0,
  "category": "Excluded | Unlikely | Possible | Probable | Highly probable"
}
```

---

## 8. Engineering Standards

### 8.1 Determinism

- Ingestion is deterministic
- Prompt versions are pinned
- Models run with temperature=0

### 8.2 Safety

- No hallucination of labs, dates, ULNs
- Conservative arbitration
- Explicit “Not reported” handling

### 8.3 Auditability

Persist:

- `case_bundle.json`
- GPT output
- Gemini output
- Arbiter output

---

## 9. Environment Variables

```bash
OPENAI_API_KEY=...
GOOGLE_API_KEY=...
OPENAI_MODEL=gpt-5.2
GEMINI_MODEL=gemini-3.0-pro
```

---

## 10. Definition of Done

A run is valid if:

- case_bundle is generated successfully
- both analysts complete Sections A/B/C
- arbiter produces final Sections A/B/C
- final JSON validates against schema
- no invented data appears in outputs

---

## 11. Intended Use

This system is intended for:

- pharmacovigilance research
- DILI case review
- RUCAM reproducibility studies

It is **not** a substitute for clinical judgment.

---
