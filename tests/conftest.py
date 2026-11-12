"""Suite-wide fixtures."""

from __future__ import annotations

import pytest

#: The modules whose `_main` reads a .env file before doing anything else.
_CLI_MODULES = (
    "dili_rucam_agents.batch",
    "dili_rucam_agents.pipeline",
    "dili_rucam_agents.verify",
)


@pytest.fixture(autouse=True)
def no_dotenv(monkeypatch):
    """Keep every CLI entry point off any real .env file.

    `find_dotenv(usecwd=True)` searches *upward* from the working directory, so
    a suite run inside a worktree reaches the checkout above it and loads the
    developer's own API keys. That pulls real credentials into the test process,
    and it makes any test that unsets a key depend on whose machine it runs on:
    `monkeypatch.delenv` removes the variable, then `load_dotenv` puts the real
    value straight back, because its default is not to override what is already
    set and the variable is no longer set.

    `load_dotenv("")` is a no-op, which is exactly what an absent file gives, so
    neutralising `find_dotenv` leaves the code path itself intact.
    """
    for module in _CLI_MODULES:
        monkeypatch.setattr(f"{module}.find_dotenv", lambda **_: "")
