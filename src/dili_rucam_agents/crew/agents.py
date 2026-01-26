from __future__ import annotations

import os
from typing import Optional

from crewai import Agent, LLM

from dili_rucam_agents.ingestion.build_bundle import CaseBundleExtractionTool


def build_ingestion_agent(model: Optional[str] = None) -> Agent:
    """Agent responsible for deterministic PDF ingestion."""

    tool = CaseBundleExtractionTool()
    ingestion_model = model or os.getenv("INGESTION_MODEL") or os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    return Agent(
        role="Deterministic PDF Ingestion Specialist",
        goal="Generate canonical case_bundle_json payloads from research PDFs without hallucination.",
        backstory=(
            "You orchestrate unstructured, pdfplumber, and PyMuPDF extractors. "
            "You never invent data and always describe missing segments."
        ),
        allow_delegation=False,
        tools=[tool],
        verbose=True,
        llm=LLM(model=ingestion_model, temperature=0),
    )


def build_rucam_agent(
    *,
    label: str,
    model_env: str,
    default_model: str,
    vendor_note: str,
) -> Agent:
    """Factory for GPT-5.2 and Gemini 3.0 analysts."""

    return Agent(
        role=f"{label} Expert DILI RUCAM Analyst",
        goal="Apply the production RUCAM prompt verbatim to case_bundle_json inputs.",
        backstory=(
            f"{label} is a board-certified hepatologist and pharmacovigilance researcher. "
            f"{vendor_note} Always compute R-ratio, determine injury pattern, score all seven RUCAM items, "
            "and output Sections A/B/C exactly as specified."
        ),
        allow_delegation=False,
        verbose=True,
        llm=LLM(model=os.getenv(model_env, default_model), temperature=0),
    )


def build_arbiter_agent(model: Optional[str] = None) -> Agent:
    """Senior hepatology arbiter to reconcile analyst outputs."""

    arbiter_model = model or os.getenv("ARBITER_MODEL") or os.getenv("OPENAI_MODEL", "gpt-5.2")
    return Agent(
        role="Senior Hepatology Arbiter",
        goal=(
            "Compare GPT and Gemini reports, resolve every discrepancy, and emit a single final RUCAM decision "
            "plus justification."
        ),
        backstory=(
            "You chaired international RUCAM harmonization panels and only side with evidence backed by the "
            "case bundle and scoring rules."
        ),
        allow_delegation=False,
        verbose=True,
        llm=LLM(model=arbiter_model, temperature=0),
    )


__all__ = [
    "build_ingestion_agent",
    "build_rucam_agent",
    "build_arbiter_agent",
]
