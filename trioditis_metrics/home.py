"""Trioditis's data folder: metrics.db, budget.json, prices.json, hardware.json,
accounts.json, plan-salt and the refit files. $HLIDSKJALV_HOME, else ~/.hlidskjalv.
Dashboards such as Lunete read some of these files; Trioditis reads nothing of theirs.

(The pre-rename ~/.arcade and $ARCADE_HOME, and the one-time move from them,
were dropped in 0.5.)
"""

from __future__ import annotations

import os
from pathlib import Path


def data_home() -> Path:
    env = os.environ.get("HLIDSKJALV_HOME")
    return Path(env) if env else Path.home() / ".hlidskjalv"
