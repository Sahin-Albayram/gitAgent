"""Shared git subprocess helper for GitAgent modules.

tools.py, init.py, and visualize.py all shell out to `git` the same way -
this used to be copy-pasted three times (visualize.py's copy raised a bare
RuntimeError instead of GitAgentError). It lives here once instead.
"""
from __future__ import annotations

import subprocess
from pathlib import Path


class GitAgentError(RuntimeError):
    """Raised when a GitAgent tool operation cannot complete."""


def run_git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise GitAgentError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    # Only trailing whitespace is stripped - leading whitespace can be
    # column-significant (e.g. `git status --porcelain`'s "XY path" format,
    # where a stray leading-strip would eat the first line's status column).
    return result.stdout.rstrip()
