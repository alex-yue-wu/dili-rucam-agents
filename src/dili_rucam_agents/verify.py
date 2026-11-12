"""Roster pre-flight: call every declared model once, before a run of seventy.

A model name checked against a vendor catalogue is not verified. Measured on
2026-11-11: ``gemini-2.5-pro`` was returned by ``models.list()``, resolved
through ``models.get()`` with ``generateContent`` among its supported actions,
and refused every call as "no longer available to new users", while
``gemini-3.1-pro-preview`` was absent from that listing and answered. A listing
is evidence of nothing in either direction, so this module calls the model.

Each entry is called the way the real run will call it -- the provider's own
starting mode, the declared ``api_version``, the real assessment schema -- so a
pass here means the transport, the key, the model name and the structured-output
mode all work together. Only the output budget differs: the probe caps it,
because reachability is the question and a frontier model's full report is not.

Nothing is persisted. Unlike the batch sinks, a failure prints the vendor's own
message, which is the whole point of running it by hand: "no longer available to
new users" is the sentence that ends the guessing, and no artifact carries it.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import find_dotenv, load_dotenv

from dili_rucam_agents.providers import (
    CompletionProvider,
    MissingApiKeyError,
    ModelSpec,
    SdkProvider,
    StructuredMode,
    model_spec,
    require_api_keys,
    starting_mode,
)
from dili_rucam_agents.roster import (
    AnalystConfig,
    AnalystRoster,
    GroundTruthConfig,
    Provider,
    RosterError,
    load_roster,
)
from dili_rucam_agents.validators.rucam_assessment import RucamAssessment

#: Says "JSON" because DeepSeek's ``json_object`` mode rejects a prompt that
#: does not, with a 400 -- which is terminal, so the real run would not retry
#: it either. Short enough that a success costs a fraction of a cent.
PROBE_PROMPT = "Return a single JSON object with one key, ok, set to true."

#: Enough that a reasoning model's hidden tokens do not swallow the whole reply,
#: little enough to be free in practice. A 256-token cap produced two spurious
#: 400s when this check was first run by hand.
PROBE_OUTPUT_TOKENS = 2000

#: The key given to the ground-truth scorer, which has none of its own.
GROUND_TRUTH_KEY = "ground_truth"


@dataclass(frozen=True)
class VerifyResult:
    """One entry's outcome, carrying what editing the roster line needs."""

    key: str
    provider: Provider
    model: str
    mode: StructuredMode
    ok: bool
    detail: str


def _probe_mode(config: AnalystConfig | GroundTruthConfig) -> StructuredMode:
    """The mode this entry will really be called in.

    The ground-truth scorer reads a published score out of free text and is
    called in ``text`` mode; an analyst starts at the highest structured mode
    its provider supports. Probing an analyst in ``text`` would pass while the
    real run fails on the schema, and probing DeepSeek in ``json_schema`` would
    fail on a rung the real run never uses.
    """
    if isinstance(config, GroundTruthConfig):
        return StructuredMode.text
    return starting_mode(config.provider)


async def _verify_one(
    key: str,
    config: AnalystConfig | GroundTruthConfig,
    provider: CompletionProvider,
    *,
    max_output_tokens: int,
    schema: dict[str, Any],
) -> VerifyResult:
    mode = _probe_mode(config)
    spec = model_spec(config).model_copy(update={"max_output_tokens": max_output_tokens})
    try:
        response = await provider.complete(
            spec,
            [{"role": "user", "content": PROBE_PROMPT}],
            mode,
            schema if mode is StructuredMode.json_schema else None,
        )
    except Exception as exc:  # noqa: BLE001 - every failure is reportable here
        status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
        prefix = f"{type(exc).__name__}"
        if status is not None:
            prefix = f"{prefix} {status}"
        return VerifyResult(
            key=key,
            provider=config.provider,
            model=config.model,
            mode=mode,
            ok=False,
            detail=f"{prefix}: {exc}",
        )
    detail = "answered"
    if response.output_tokens is not None:
        detail = f"{detail}, out={response.output_tokens}"
    if response.truncated:
        # Expected: the probe's budget is small on purpose. It still answered.
        detail = f"{detail}, truncated at the probe's own cap"
    return VerifyResult(
        key=key,
        provider=config.provider,
        model=config.model,
        mode=mode,
        ok=True,
        detail=detail,
    )


def verify_roster(
    roster: AnalystRoster,
    provider: CompletionProvider,
    *,
    max_output_tokens: int = PROBE_OUTPUT_TOKENS,
    include_ground_truth: bool = True,
) -> list[VerifyResult]:
    """Call every entry once, in roster order, and report each outcome.

    One failure never stops the pass: the point is to learn every bad name in
    one round trip rather than one per attempt. Sequential, like the analyst
    tier, so a rate limit cannot be mistaken for a bad model name.
    """
    entries: list[tuple[str, AnalystConfig | GroundTruthConfig]] = [
        (config.key, config) for config in roster.analysts
    ]
    if include_ground_truth:
        entries.append((GROUND_TRUTH_KEY, roster.ground_truth))
    schema = RucamAssessment.model_json_schema()

    async def run() -> list[VerifyResult]:
        try:
            return [
                await _verify_one(
                    key,
                    config,
                    provider,
                    max_output_tokens=max_output_tokens,
                    schema=schema,
                )
                for key, config in entries
            ]
        finally:
            await provider.aclose()

    return asyncio.run(run())


def _print_table(results: Sequence[VerifyResult]) -> None:
    width = max(len(r.key) for r in results)
    for result in results:
        mark = "OK  " if result.ok else "FAIL"
        print(
            f"{mark} {result.key:{width}s}  {result.provider.value:11s}"
            f"{result.model:26s} mode={result.mode.value:12s} {result.detail}"
        )


def _main(
    argv: Sequence[str] | None = None,
    *,
    provider: CompletionProvider | None = None,
) -> None:
    # As in batch._main: read a .env beside the user's work, never this file's
    # directory, and let an exported value win.
    load_dotenv(find_dotenv(usecwd=True))
    parser = argparse.ArgumentParser(
        description=(
            "Call every model in an analyst roster once, to check the key, the "
            "model name and the structured-output mode before a paid run."
        )
    )
    parser.add_argument(
        "--analysts",
        dest="roster_path",
        metavar="PATH",
        help=(
            "Analyst roster JSON (default: config/analysts.json, one analyst). "
            "config/analysts.study.json declares the seven-analyst study roster."
        ),
    )
    parser.add_argument(
        "--no-ground-truth",
        dest="include_ground_truth",
        action="store_false",
        help="Skip the ground-truth scorer, which only --mask-scores calls.",
    )
    parser.add_argument(
        "--max-output-tokens",
        dest="max_output_tokens",
        type=int,
        default=PROBE_OUTPUT_TOKENS,
        help=f"Output cap for each probe call (default: {PROBE_OUTPUT_TOKENS}).",
    )
    args = parser.parse_args(argv)

    try:
        roster = load_roster(Path(args.roster_path) if args.roster_path else None)
    except RosterError as exc:
        raise SystemExit(str(exc)) from exc

    needed = {config.provider for config in roster.analysts}
    if args.include_ground_truth:
        needed.add(roster.ground_truth.provider)
    try:
        require_api_keys(needed)
    except MissingApiKeyError as exc:
        # Before any call: being told a key is unset is not worth a round trip.
        raise SystemExit(str(exc)) from exc

    results = verify_roster(
        roster,
        provider if provider is not None else SdkProvider(),
        max_output_tokens=args.max_output_tokens,
        include_ground_truth=args.include_ground_truth,
    )
    _print_table(results)
    failed = [result for result in results if not result.ok]
    if failed:
        print(f"\n{len(failed)} of {len(results)} entries did not answer.")
        raise SystemExit(1)
    print(f"\nall {len(results)} entries answered.")


__all__ = [
    "GROUND_TRUTH_KEY",
    "PROBE_OUTPUT_TOKENS",
    "PROBE_PROMPT",
    "VerifyResult",
    "verify_roster",
]
