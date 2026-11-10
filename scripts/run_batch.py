"""Batch CLI entry point.

Every flag, ``--analysts`` included, is declared by ``batch._main``; this file
exists so the batch driver can be started as a script.
"""

from dili_rucam_agents.batch import _main


if __name__ == "__main__":
    _main()
