"""Minimal HTTP access for the networked evidence tiers.

Fetchers are passed into the resolvers rather than called by them, so tests
drive the whole cascade from fixtures and never open a socket.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping
from urllib.parse import urlencode
from urllib.request import Request, urlopen

#: Generic client name. The user's own identity is never sent to these services.
USER_AGENT = "dili-rucam-agents/0.1 (RUCAM causality assessment pipeline)"
DEFAULT_TIMEOUT_SECONDS = 30.0
#: Guard against an unexpectedly large response being parsed into memory.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024

TextFetcher = Callable[[str], str]
JsonFetcher = Callable[[str], Any]


class FetchError(RuntimeError):
    """Raised when a source cannot be reached or returns an unusable body."""


def build_url(base: str, params: Mapping[str, Any]) -> str:
    """Append `params` to `base` in sorted order so URLs are reproducible."""

    if not params:
        return base
    encoded = urlencode(sorted((key, str(value)) for key, value in params.items()))
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}{encoded}"


def fetch_text(url: str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> str:
    """GET `url` and decode the body as UTF-8."""

    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed https hosts
            payload = response.read(MAX_RESPONSE_BYTES + 1)
    except Exception as error:  # noqa: BLE001 - every transport failure is the same here
        raise FetchError(f"GET {url} failed: {error}") from error
    if len(payload) > MAX_RESPONSE_BYTES:
        raise FetchError(f"GET {url} returned more than {MAX_RESPONSE_BYTES} bytes")
    return payload.decode("utf-8", errors="replace")


def fetch_json(url: str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> Any:
    """GET `url` and parse the body as JSON."""

    body = fetch_text(url, timeout=timeout)
    try:
        return json.loads(body)
    except json.JSONDecodeError as error:
        raise FetchError(f"GET {url} did not return JSON: {error}") from error


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "FetchError",
    "JsonFetcher",
    "MAX_RESPONSE_BYTES",
    "TextFetcher",
    "USER_AGENT",
    "build_url",
    "fetch_json",
    "fetch_text",
]
