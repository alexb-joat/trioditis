"""Metering for Trioditis: the call record, the budget guard and the price table.

The names below load on first use, not with the package: db, guard and prices fix their
default paths in the data folder when imported, and `serve` must be able to move that
folder first (`trioditis migrate`) by importing only trioditis_metrics.home.
"""

from importlib import import_module

_WHERE = {"MetricsDB": ".db", "BudgetCaps": ".guard", "BudgetGuard": ".guard", "Prices": ".prices"}

__all__ = ["MetricsDB", "BudgetCaps", "BudgetGuard", "Prices"]


def __getattr__(name: str):
    if name in _WHERE:
        return getattr(import_module(_WHERE[name], __name__), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
