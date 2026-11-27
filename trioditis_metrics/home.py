"""Trioditis's data folder: metrics.db, budget.json, prices.json, hardware.json,
accounts.json, plan-salt and the refit files.

$TRIODITIS_HOME, else the old $HLIDSKJALV_HOME (with a one-line warning), else
~/.trioditis. An install from before 0.9.1 that has ~/.hlidskjalv and no ~/.trioditis
keeps using ~/.hlidskjalv, so an upgrade never starts from an empty folder.
Dashboards such as Lunete read some of these files; Trioditis reads nothing of theirs.

(The pre-rename ~/.arcade and $ARCADE_HOME, and the one-time move from them,
were dropped in 0.5.)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_warned = False


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
    if not home.exists() and legacy.exists():
        return legacy
    return home


def _warn_once(message: str) -> None:
    global _warned
    if not _warned:
        _warned = True
        print(message, file=sys.stderr)
