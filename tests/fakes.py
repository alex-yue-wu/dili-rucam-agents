"""Shared test doubles. ``FakeProvider`` implements only ``CompletionProvider``."""

from __future__ import annotations

from collections import deque
from typing import Any

from dili_rucam_agents.providers import ModelResponse, ModelSpec, StructuredMode


class StatusError(Exception):
    """An SDK-shaped error: an integer ``status_code`` and a message."""

    def __init__(self, status_code: int, message: str = "error") -> None:
        super().__init__(message)
        self.status_code = status_code


class FakeProvider:
    """Replays scripted outcomes in order: a ``ModelResponse`` or an exception.

    ``queue`` accepts either, so a test reads as the sequence of provider
    behaviour it wants. An exhausted script fails loudly rather than inventing
    a reply. Every call is recorded in ``calls`` as ``(spec, messages, mode,
    schema)``.
    """

    def __init__(self, *script: ModelResponse | BaseException | str) -> None:
        self._script: deque[ModelResponse | BaseException] = deque()
        self.calls: list[tuple[ModelSpec, list[dict[str, str]], StructuredMode, dict[str, Any] | None]] = []
        self.queue(*script)

    def queue(self, *items: ModelResponse | BaseException | str) -> FakeProvider:
        """Append outcomes; a bare string is shorthand for a complete reply."""
        for item in items:
            self._script.append(ModelResponse(text=item) if isinstance(item, str) else item)
        return self

    @property
    def last_messages(self) -> list[dict[str, str]]:
        return self.calls[-1][1]

    @property
    def modes(self) -> list[StructuredMode]:
        return [call[2] for call in self.calls]

    async def complete(
        self,
        spec: ModelSpec,
        messages: list[dict[str, str]],
        mode: StructuredMode,
        schema: dict[str, Any] | None,
    ) -> ModelResponse:
        self.calls.append((spec, [dict(m) for m in messages], mode, schema))
        if not self._script:
            raise AssertionError("FakeProvider script exhausted")
        item = self._script.popleft()
        if isinstance(item, BaseException):
            raise item
        return item
