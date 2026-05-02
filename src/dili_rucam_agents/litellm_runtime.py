from __future__ import annotations

from typing import Any

import litellm
from litellm.litellm_core_utils import litellm_logging


_PATCHED = False


def configure_litellm_runtime() -> None:
    """Disable optional LiteLLM logging paths that pull proxy-only dependencies."""

    global _PATCHED
    if _PATCHED:
        return

    def _disabled_standard_logging_object_payload(*args: Any, **kwargs: Any) -> None:
        return None

    litellm_logging.get_standard_logging_object_payload = (
        _disabled_standard_logging_object_payload
    )
    litellm.service_callback = []
    litellm.success_callback = []
    litellm.failure_callback = []
    litellm.callbacks = []
    litellm.turn_off_message_logging = True
    _PATCHED = True


__all__ = ["configure_litellm_runtime"]
