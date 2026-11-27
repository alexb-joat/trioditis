"""Trioditis's data folder: metrics.db, budget.json, prices.json, hardware.json,
accounts.json, plan-salt and the refit files.

$TRIODITIS_HOME, else the old $HLIDSKJALV_HOME (with a one-line warning), else
~/.trioditis. An install from before 0.9.1 whose Trioditis files are still in
~/.hlidskjalv, with no ~/.trioditis yet, keeps using ~/.hlidskjalv, so an upgrade never
starts from an empty folder; `trioditis migrate` (also run when `serve` starts) moves them.
Dashboards such as Lunete read some of these files; Trioditis reads nothing of theirs.

(The pre-rename ~/.arcade and $ARCADE_HOME, and the one-time move from them,
were dropped in 0.5.)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# The files Trioditis owns in its data folder: `trioditis migrate` moves exactly these.
# Everything else in an old shared folder (Lunete's files) stays where it is.
OWNED_FILES = ("metrics.db", "budget.json", "prices.json", "hardware.json", "accounts.json",
               "plan-salt", "trioditis-fitted.json", "trioditis-notebook.jsonl", "trioditis-adopted.json")
OWNED_DIRS = ("trials",)
OWNED_PREFIXES = ("prices.json.bak",)  # hand-made backups move with prices.json

_warned = False


def owned_names(folder: Path) -> list[str]:
    """The Trioditis entries present in `folder`, in a fixed order (metrics.db first)."""
    try:
        present = {p.name for p in folder.iterdir()}
    except OSError:
        return []
    names = [n for n in OWNED_FILES + OWNED_DIRS if n in present]
    return names + sorted(n for n in present if n.startswith(OWNED_PREFIXES) and n not in names)


def data_home() -> Path:
    new = os.environ.get("TRIODITIS_HOME")
    if new:
        return Path(new)
    old = os.environ.get("HLIDSKJALV_HOME")
    if old:
        _warn_once("warning: HLIDSKJALV_HOME is deprecated and will be removed; "
                   "rename it to TRIODITIS_HOME (same value)")
        return Path(old)
    home = Path.home() / ".trioditis"
    legacy = Path.home() / ".hlidskjalv"
    if not home.exists() and owned_names(legacy):
        return legacy
    return home


def _warn_once(message: str) -> None:
    global _warned
    if not _warned:
        _warned = True
        print(message, file=sys.stderr)
