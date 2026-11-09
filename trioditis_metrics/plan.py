"""Plan usage: the store and the reports.

A subscription plan (Claude, ChatGPT) has usage meters instead of a bill. To learn how heavy each
model and effort is on those meters, Trioditis keeps two things apart from its API calls:

- `plan_usage`: one row per message a chat or agent sent on the plan, with token counts, model,
  effort and hashes. Never any text.
- `plan_meter`: the meter readings (percent of each window used) over time.

The importers live in `trioditis.plan`; this module only stores and reports.
"""

from __future__ import annotations

import sqlite3

# Columns an event may carry, in table order (`id` and `at` come first).
EVENT_COLUMNS = ("source", "person", "service", "model", "effort", "task", "tokens_in", "tokens_out",
                 "cache_read", "cache_write", "tokens_measured", "speed", "agent", "thread_turns",
                 "thread_tokens", "thread_fp", "suggested", "followed", "regenerated", "asked_again", "vote")
# Counts can only grow as a message is written out, so a merge keeps the larger value.
COUNTS = ("tokens_in", "tokens_out", "cache_read", "cache_write", "thread_turns", "thread_tokens")

GROUPS = {
    "model-effort": "model || ' ' || COALESCE(effort, '-')",
    "model": "model",
    "effort": "COALESCE(effort, '-')",
    "day": "date(at, 'unixepoch', 'localtime')",
    "source": "source",
    "person": "COALESCE(person, '-')",
    "agent": "CASE agent WHEN 1 THEN 'subagent' WHEN 0 THEN 'main' ELSE '-' END",
}

# A reading whose reset time moved by more than this belongs to a new window.
NEW_WINDOW_S = 600


def has_tables(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='plan_usage'").fetchone() is not None


def merge_events(conn: sqlite3.Connection, events: list[dict], write: bool = True) -> dict:
    """Add new events and complete known ones (larger counts win, missing fields get filled).
    Returns {added, updated, unchanged}. With write=False nothing is changed."""
    out = {"added": 0, "updated": 0, "unchanged": 0}
    have_tables = has_tables(conn)
    for ev in events:
        old = conn.execute("SELECT * FROM plan_usage WHERE id = ?", (ev["id"],)).fetchone() if have_tables else None
        if old is None:
            out["added"] += 1
            if write:
                cols = ("id", "at", *EVENT_COLUMNS)
                conn.execute(f"INSERT INTO plan_usage ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",  # noqa: S608
                             [ev.get(c) for c in cols])
            continue
        old = dict(old)
        new = {}
        for c in EVENT_COLUMNS:
            v = ev.get(c)
            if v is None or v == old[c]:
                continue
            if c in COUNTS and old[c] is not None and v <= old[c]:
                continue
            if c not in COUNTS and old[c] is not None:
                continue  # a label already set stays; only counts move
            new[c] = v
        if ev["at"] < old["at"]:
            new["at"] = ev["at"]
        if not new:
            out["unchanged"] += 1
            continue
        out["updated"] += 1
        if write:
            # Column names come from EVENT_COLUMNS; every value is a bound parameter.
            conn.execute(f"UPDATE plan_usage SET {', '.join(f'{c} = ?' for c in new)} WHERE id = ?",  # noqa: S608
                         [*new.values(), ev["id"]])
    return out


def merge_meter(conn: sqlite3.Connection, readings: list[dict], write: bool = True) -> dict:
    """Add meter readings not seen before. Returns {added, unchanged}."""
    out = {"added": 0, "unchanged": 0}
    have_tables = has_tables(conn)
    for r in readings:
        key = (r["meter"], r.get("model") or "", r["at"])
        seen = have_tables and conn.execute(
            "SELECT 1 FROM plan_meter WHERE meter = ? AND model = ? AND at = ?", key).fetchone()
        if seen:
            out["unchanged"] += 1
            continue
        out["added"] += 1
        if write:
            conn.execute("INSERT INTO plan_meter (meter, model, at, percent, resets_at) VALUES (?,?,?,?,?)",
                         (*key[:2], r["at"], r["percent"], r.get("resets_at")))
    return out


def file_seen(conn: sqlite3.Connection, path_fp: str, size: int, mtime: float) -> bool:
    if not has_tables(conn):
        return False
    row = conn.execute("SELECT size, mtime FROM plan_files WHERE path_fp = ?", (path_fp,)).fetchone()
    return row is not None and row[0] == size and row[1] == mtime


def mark_file(conn: sqlite3.Connection, path_fp: str, size: int, mtime: float) -> None:
    conn.execute("INSERT INTO plan_files (path_fp, size, mtime) VALUES (?,?,?) ON CONFLICT(path_fp) "
                 "DO UPDATE SET size = excluded.size, mtime = excluded.mtime", (path_fp, size, mtime))


def plan_usage(conn: sqlite3.Connection, since: float, until: float | None = None,
               by: str = "model-effort") -> list[dict]:
    """Messages and tokens per group between `since` and `until`, most output tokens first."""
    if by not in GROUPS:
        raise ValueError(f"cannot group plan usage by {by!r}; choose from {', '.join(GROUPS)}")
    if not has_tables(conn):
        return []
    where, args = "at >= ?", [since]
    if until is not None:
        where += " AND at < ?"
        args.append(until)
    # The key comes from GROUPS and the filter is fixed text; values are bound parameters.
    return [dict(r) for r in conn.execute(
        f"SELECT {GROUPS[by]} AS key, COUNT(*) AS messages, "  # noqa: S608
        "COALESCE(SUM(tokens_in), 0) AS tokens_in, COALESCE(SUM(cache_read), 0) AS cache_read, "
        "COALESCE(SUM(cache_write), 0) AS cache_write, COALESCE(SUM(tokens_out), 0) AS tokens_out, "
        "SUM(tokens_measured) AS measured, MIN(at) AS first_ts, MAX(at) AS last_ts "
        f"FROM plan_usage WHERE {where} GROUP BY 1 ORDER BY tokens_out DESC, messages DESC, key", args)]


def meter_rise(conn: sqlite3.Connection, since: float, until: float | None = None) -> list[dict]:
    """Per meter, how many points it rose between `since` and `until`: the sum of the rises between
    consecutive readings. When a window resets (the percent drops, or the reset time moves), the new
    window's reading counts from zero. Use before the first reading isn't seen."""
    if not has_tables(conn):
        return []
    where, args = "at >= ?", [since]
    if until is not None:
        where += " AND at < ?"
        args.append(until)
    out: dict[tuple, dict] = {}
    for meter, model, _at, pct, resets_at in conn.execute(
            f"SELECT meter, model, at, percent, resets_at FROM plan_meter WHERE {where} "  # noqa: S608
            "ORDER BY meter, model, at", args):
        m = out.get((meter, model))
        if m is None:
            out[(meter, model)] = {"meter": meter, "model": model or None, "readings": 1, "first": pct, "last": pct,
                                   "rise": 0.0, "resets": 0, "_ra": resets_at}
            continue
        m["readings"] += 1
        moved = resets_at is not None and m["_ra"] is not None and abs(resets_at - m["_ra"]) > NEW_WINDOW_S
        if pct < m["last"] or moved:
            m["rise"] += pct
            m["resets"] += 1
        else:
            m["rise"] += pct - m["last"]
        m["last"], m["_ra"] = pct, resets_at if resets_at is not None else m["_ra"]
    for m in out.values():
        del m["_ra"]
    return list(out.values())


def _window(since: float, until: float | None) -> tuple[str, list]:
    if until is None:
        return "at >= ?", [since]
    return "at >= ? AND at < ?", [since, until]


def meter_readings(conn: sqlite3.Connection, since: float, until: float | None = None) -> dict[tuple, list[tuple]]:
    """Every reading between `since` and `until`, as {(meter, model): [(at, percent, resets_at), ...]} in time
    order. `model` is "" for a meter that counts every model."""
    if not has_tables(conn):
        return {}
    where, args = _window(since, until)
    out: dict[tuple, list[tuple]] = {}
    for meter, model, at, pct, resets_at in conn.execute(
            f"SELECT meter, model, at, percent, resets_at FROM plan_meter WHERE {where} "  # noqa: S608
            "ORDER BY meter, model, at", args):
        out.setdefault((meter, model), []).append((at, pct, resets_at))
    return out


def plan_messages(conn: sqlite3.Connection, since: float, until: float | None = None) -> list[dict]:
    """Each message's time, model, effort and token counts (missing counts as 0), in time order."""
    if not has_tables(conn):
        return []
    where, args = _window(since, until)
    # The filter is fixed text from _window; values are bound parameters.
    return [dict(r) for r in conn.execute(
        "SELECT at, model, effort, COALESCE(tokens_in, 0) AS tokens_in, COALESCE(tokens_out, 0) AS tokens_out, "  # noqa: S608
        "COALESCE(cache_read, 0) AS cache_read, COALESCE(cache_write, 0) AS cache_write "
        f"FROM plan_usage WHERE {where} ORDER BY at", args)]


def signal_counts(conn: sqlite3.Connection, since: float, until: float | None = None) -> list[dict]:
    """Per model and effort, how often a send carried each result signal, and how often it was positive:
    a suggestion followed, an answer regenerated, the question asked again, an up vote. A signal the
    exporter didn't record (NULL) is left out of its count; a vote of 0 is no vote."""
    if not has_tables(conn):
        return []
    where, args = _window(since, until)
    return [dict(r) for r in conn.execute(
        f"SELECT {GROUPS['model-effort']} AS key, COUNT(*) AS messages, "  # noqa: S608
        "SUM(suggested IS NOT NULL AND followed IS NOT NULL) AS followed_n, "
        "COALESCE(SUM(CASE WHEN suggested IS NOT NULL THEN followed = 1 END), 0) AS followed, "
        "SUM(regenerated IS NOT NULL) AS regenerated_n, COALESCE(SUM(regenerated = 1), 0) AS regenerated, "
        "SUM(asked_again IS NOT NULL) AS asked_again_n, COALESCE(SUM(asked_again = 1), 0) AS asked_again, "
        "SUM(vote IS NOT NULL AND vote != 0) AS vote_n, COALESCE(SUM(vote > 0), 0) AS vote_up "
        f"FROM plan_usage WHERE {where} GROUP BY 1 ORDER BY 1", args)]
