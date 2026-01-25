from __future__ import annotations

import os
from pathlib import Path
from textwrap import dedent
from typing import Optional

from crewai import Agent, Crew, LLM, Process, Task

from .tools import CaseBundleExtractionTool


DEFAULT_PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "rucam_analysis_production.md"


class RucamCrewBuilder:
    """Factory that wires the deterministic ingestion, dual analysts, and arbiter."""

    def __init__(self, prompt_path: Optional[Path] = None) -> None:
        self.prompt_path = prompt_path or DEFAULT_PROMPT_PATH
        self._prompt_cache: Optional[str] = None

    def build(self, pdf_path: str) -> Crew:
        """Return a Crew configured for the requested PDF path."""
        pdf_tool = CaseBundleExtractionTool()

        ingestion_agent = self._build_ingestion_agent(pdf_tool)
        gpt_agent = self._build_rucam_agent(
            agent_name="GPT-5.1 RUCAM Analyst",
            model_env="OPENAI_MODEL",
            default_model="gpt-5.1",
            vendor_note="OpenAI GPT-5.1 deterministic reasoning model.",
        )
        gemini_agent = self._build_rucam_agent(
            agent_name="Gemini 3.0 RUCAM Analyst",
            model_env="GEMINI_MODEL",
            default_model="gemini-3.0-pro",
            vendor_note="Google Gemini 3.0 dual-encoder causal reasoning model.",
        )
        arbiter_agent = self._build_arbiter_agent()

        case_bundle_task = Task(
            name="case_bundle_generation",
            description=dedent(
                f"""
                Deterministically ingest the clinical PDF located at "{pdf_path}".
                Use the `case_bundle_extractor` tool to produce the canonical `case_bundle_json`
                contract defined in agent.md. Preserve every page and do not invent data.
                """
            ).strip(),
            expected_output=(
                "A valid JSON object named `case_bundle_json` that matches the schema from agent.md, "
                "including pdf_path, extraction_notes, blocks, normalized_text, tables, unknowns, and quality."
            ),
            agent=ingestion_agent,
            inputs={"pdf_path": pdf_path},
        )

        gpt_task = Task(
            name="gpt51_rucam_analysis",
            description=self._analysis_task_description(
                analyst_label="GPT-5.1",
                model_reference="OPENAI_MODEL",
            ),
            expected_output=self._analysis_expected_output("GPT-5.1"),
            context=[case_bundle_task],
            agent=gpt_agent,
        )

        gemini_task = Task(
            name="gemini_rucam_analysis",
            description=self._analysis_task_description(
                analyst_label="Gemini 3.0",
                model_reference="GEMINI_MODEL",
            ),
            expected_output=self._analysis_expected_output("Gemini 3.0"),
            context=[case_bundle_task],
            agent=gemini_agent,
        )

        arbiter_task = Task(
            name="senior_hepatology_arbitration",
            description=self._arbiter_task_description(),
            expected_output=self._arbiter_expected_output(),
            context=[gpt_task, gemini_task],
            agent=arbiter_agent,
        )

        return Crew(
            agents=[ingestion_agent, gpt_agent, gemini_agent, arbiter_agent],
            tasks=[case_bundle_task, gpt_task, gemini_task, arbiter_task],
            process=Process.sequential,
            verbose=2,
        )

    def run(self, pdf_path: str, **kwargs) -> str:
        """Build and execute the crew for the given PDF path."""
        crew = self.build(pdf_path)
        return crew.kickoff(inputs={"pdf_path": pdf_path, **kwargs})

    def _build_ingestion_agent(self, pdf_tool: CaseBundleExtractionTool) -> Agent:
        return Agent(
            role="Deterministic PDF Ingestion Specialist",
            goal="Generate auditable case_bundle_json payloads from research PDFs without hallucination.",
            backstory=(
                "You orchestrate a deterministic ingestion stack (unstructured, pdfplumber, PyMuPDF) "
                "and only rely on tool outputs. You never invent data and you highlight extraction gaps."
            ),
            tools=[pdf_tool],
            allow_delegation=False,
            verbose=True,
            llm=LLM(
                model=os.getenv("INGESTION_MODEL", os.getenv("OPENAI_MODEL", "gpt-4o-mini")),
                temperature=0,
            ),
        )

    def _build_rucam_agent(
        self,
        *,
        agent_name: str,
        model_env: str,
        default_model: str,
        vendor_note: str,
    ) -> Agent:
        return Agent(
            role="Expert DILI RUCAM Analyst",
            goal=(
                f"Apply the production RUCAM prompt verbatim to case_bundle_json inputs and produce "
                f"auditable Sections A/B/C as {agent_name}."
            ),
            backstory=(
                f"{agent_name} is a board-certified hepatologist and pharmacovigilance researcher. "
                f"{vendor_note} Always compute R-ratio, determine injury pattern, and adhere to "
                f"structured outputs. Use only information present in the case bundle."
            ),
            allow_delegation=False,
            verbose=True,
            llm=LLM(
                model=os.getenv(model_env, default_model),
                temperature=0,
            ),
        )

    def _build_arbiter_agent(self) -> Agent:
        return Agent(
            role="Senior Hepatology Arbiter",
            goal=(
                "Compare both analyst reports, resolve every discrepancy, and produce a single authoritative "
                "final RUCAM report plus justification of arbitration decisions."
            ),
            backstory=(
                "You chaired international RUCAM harmonization panels. "
                "You only side with evidence backed by the case bundle and RUCAM rules."
            ),
            allow_delegation=False,
            verbose=True,
            llm=LLM(
                model=os.getenv("ARBITER_MODEL", os.getenv("OPENAI_MODEL", "gpt-5.1")),
                temperature=0,
            ),
        )

    def _analysis_task_description(self, *, analyst_label: str, model_reference: str) -> str:
        return dedent(
            f"""
            You are the {analyst_label} RUCAM Analyst. Consume the shared `case_bundle_json` exactly as produced.
            Follow every instruction in rucam_analysis_production.md (attached below) without deviation.

            --- BEGIN PRODUCTION PROMPT ---
            {self._rucam_prompt}
            --- END PRODUCTION PROMPT ---

            Reference your model via the `{model_reference}` environment variable. Temperature must remain 0.
            """
        ).strip()

    def _analysis_expected_output(self, analyst_label: str) -> str:
        return (
            f"A complete {analyst_label} report with SECTION A narrative, SECTION B scoring table, "
            "and SECTION C JSON exactly matching the production specification."
        )

    def _arbiter_task_description(self) -> str:
        return dedent(
            """
            Review the GPT-5.1 and Gemini 3.0 reports. Identify every disagreement in data extraction,
            R-ratio, injury pattern, and each RUCAM item. Resolve conflicts strictly according to the
            case_bundle evidence. Produce a final report in the same SECTION A/B/C format plus an
            additional SECTION D — Arbiter Justification explaining why you selected the final scores
            whenever discrepancies existed.
            """
        ).strip()

    def _arbiter_expected_output(self) -> str:
        return (
            "SECTION A/B/C final report that mirrors the production template plus SECTION D — Arbiter "
            "Justification containing explicit rationale for each conflict."
        )

    @property
    def _rucam_prompt(self) -> str:
        if self._prompt_cache is None:
            self._prompt_cache = self.prompt_path.read_text(encoding="utf-8")
        return self._prompt_cache


__all__ = ["RucamCrewBuilder"]
