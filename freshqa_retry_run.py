#!/usr/bin/env python3
"""FreshQA retry-flow cassette harness — thin shim. SeaWeb owns the real one.

The harness drives SeaWeb internals (workers.research.fetch_lane, .discovery,
livestore, gateway.live_read), so it lived in the wrong repo: SeaWeb's
tests/test_freshqa_cassette.py had to import it back across the workspace, which
made that test skip in every CI clone and worktree and run only where a session
editing this repo could break it. It now lives at
SeaWeb/tests/_freshqa_retry_harness.py with its cassettes in
SeaWeb/tests/fixtures/, and this module re-exports it so the CLI and any local
scripts keep working.

Usage is unchanged:
  python freshqa_retry_run.py --cassette <path> --questions questions.json
  python freshqa_retry_run.py    # bundled 5-question smoke set

Cassette edits belong in SeaWeb/tests/fixtures/ now — a copy here would go stale
against the test that pins its schema.
"""
from __future__ import annotations

import sys
from pathlib import Path


def _seaweb_root() -> Path:
    """Locate the SeaWeb checkout that owns the harness."""
    here = Path(__file__).resolve()
    for base in (here.parent.parent, Path.cwd(), Path.cwd().parent):
        cand = base / "SeaWeb"
        if (cand / "tests" / "_freshqa_retry_harness.py").is_file():
            return cand
    raise ModuleNotFoundError(
        "SeaWeb checkout not found next to this repo. The FreshQA harness moved "
        "into SeaWeb (tests/_freshqa_retry_harness.py) on 2026-08-21; this file "
        "is only a shim. Clone SeaWeb beside tavily-search-evals, or run the "
        "harness from SeaWeb directly."
    )


_SEAWEB = _seaweb_root()
for _entry in (str(_SEAWEB), str(_SEAWEB / "gateway")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from tests._freshqa_retry_harness import *  # noqa: F401,F403,E402
from tests._freshqa_retry_harness import (  # noqa: E402
    DEFAULT_CASSETTE,
    SMOKE_QUESTIONS,
    WIKI_V1_CASSETTE,
    load_cassette,
    main,
    run,
)

__all__ = [
    "DEFAULT_CASSETTE",
    "WIKI_V1_CASSETTE",
    "SMOKE_QUESTIONS",
    "load_cassette",
    "main",
    "run",
]

if __name__ == "__main__":
    raise SystemExit(main())
