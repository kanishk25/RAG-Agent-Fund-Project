"""Identify which index version is loaded (P1.5 / P6.9).

Because the index is committed to git (ARCH §8.2), the SHA of the last commit
touching `data/` names the exact corpus being served. That makes two things
possible:

  - Ops: `GET /health` exposes it, so "the workflow commits daily but the API
    still serves an old checkout" (the P6 silent-staleness failure) is visible
    rather than invisible.
  - Eval: runs pin an index SHA, so two scorecards are comparable and a delta
    reflects a code change rather than yesterday's NAV moving (eval.md §2.1).

Every failure path returns a sentinel rather than raising. A missing git binary
or an un-initialised repo must not take the API down — this is metadata, not a
serving dependency.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from mf_faq.settings import REPO_ROOT

UNKNOWN = "unknown"


def _git(*args: str, cwd: Path = REPO_ROOT) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def index_sha(short: bool = True) -> str:
    """SHA of the last commit that touched `data/`, or 'unknown'.

    Not simply HEAD: a code-only commit does not change the corpus, so HEAD
    would report a new index version when nothing about the data changed.
    """
    fmt = "%h" if short else "%H"
    return _git("log", "-1", f"--format={fmt}", "--", "data") or UNKNOWN


def index_committed_at() -> str | None:
    """ISO timestamp of the last index commit, for staleness display."""
    return _git("log", "-1", "--format=%cI", "--", "data")


def is_git_repo() -> bool:
    return _git("rev-parse", "--is-inside-work-tree") == "true"
