"""Roster pre-flight CLI entry point.

Every flag is declared by ``verify._main``; this file exists so the pre-flight
can be started as a script, as ``run_batch.py`` does for the batch driver.
"""

from dili_rucam_agents.verify import _main


if __name__ == "__main__":
    _main()
