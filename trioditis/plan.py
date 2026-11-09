"""`trioditis plan import`: plan usage from Claude Code's transcripts, from a plan-usage.v1 events file
(Euboulia exports one), and plan meter readings from a file.

What is kept: token counts, model, effort, speed, whether a subagent sent it, the time, the message's
position and context size in its thread, and salted hashes of the message and session ids. What is
never read into the database: message content, folder names, paths, branches, titles or URLs. Nothing
here makes a network call.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import secrets
import sys
import time
from pathlib import Path

from trioditis_metrics.db import DEFAULT_PATH, MetricsDB, open_readonly
from trioditis_metrics.home import data_home
from trioditis_metrics.plan import file_seen, mark_file, merge_events, merge_meter

EVENTS_FORMAT = "plan-usage.v1"
METER_FORMAT = "plan-meter.v1"
# Up to three short words: model names ("Opus 5.5 Max"), labels and ids pass; sentences don't.
_WORDS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+()-]*( [A-Za-z0-9._:/+()-]+){0,2}$")
_MAX_LEN = 40
_STR = ("source", "person", "service", "model", "effort", "task", "speed", "thread_fp", "suggested")
_INT = {"tokens_in_est": "tokens_in", "tokens_out_est": "tokens_out", "thread_tokens_est": "thread_tokens",
        "thread_turns": "thread_turns", "vote": "vote", "cache_read": "cache_read", "cache_write": "cache_write"}
_BOOL = ("tokens_measured", "followed", "regenerated", "asked_again", "agent")


def salt_path() -> Path:
    return data_home() / "plan-salt"


def load_salt(path: Path | None = None, create: bool = True) -> str:
    """A random secret per computer, so the hashes can't be matched to ids found elsewhere."""
    path = path or salt_path()
    try:
        return path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        if not create:
            return "dry-run"  # a dry run writes nothing, not even the salt
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write(secrets.token_hex(32))
    return path.read_text(encoding="ascii").strip()


def _hash(salt: str, text: str, n: int = 24) -> str:
    return hashlib.sha256(f"{salt}\0{text}".encode("utf-8", "ignore")).hexdigest()[:n]


def _epoch(v) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) / 1000 if v > 1e11 else float(v)  # milliseconds, as JavaScript writes them
    if isinstance(v, str):
        try:
            return dt.datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _word(v) -> str | None:
    return v if isinstance(v, str) and len(v) <= _MAX_LEN and _WORDS.match(v) else None


def _count(v) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) and 0 <= v < 10**10 else None


# --- Claude Code transcripts ---------------------------------------------------------------------

def claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser()


def transcript_events(path: Path, salt: str, person: str | None = None) -> list[dict]:
    """One event per assistant message in a transcript. A message is written over several lines as it
    streams; its counts only grow, so each keeps the largest."""
    by_id: dict[str, dict] = {}
    sessions: dict[str, str] = {}
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if '"assistant"' not in line:
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if not isinstance(o, dict) or o.get("type") != "assistant" or not isinstance(o.get("message"), dict):
                continue
            m = o["message"]
            model, mid = m.get("model"), m.get("id")
            if not isinstance(model, str) or model.startswith("<") or not isinstance(mid, str):
                continue  # "<synthetic>": Claude Code's own notices, no model call
            u = m.get("usage") if isinstance(m.get("usage"), dict) else {}
            at = _epoch(o.get("timestamp"))
            if at is None:
                continue
            counts = {"tokens_in": _count(u.get("input_tokens")), "tokens_out": _count(u.get("output_tokens")),
                      "cache_read": _count(u.get("cache_read_input_tokens")),
                      "cache_write": _count(u.get("cache_creation_input_tokens"))}
            ev = by_id.get(mid)
            if ev is None:
                ev = by_id[mid] = {
                    "id": "cc-" + _hash(salt, mid), "at": at, "source": "claude-code", "person": person,
                    "service": "claude", "model": _word(model), "effort": _word(o.get("effort") or o.get("perTurnEffort")),
                    "task": None, "speed": _word(u.get("speed")), "agent": 1 if o.get("isSidechain") else 0,
                    "tokens_measured": 1, **counts}
                if isinstance(o.get("sessionId"), str):
                    sessions[mid] = o["sessionId"]
                continue
            ev["at"] = min(ev["at"], at)
            for k, v in counts.items():
                if v is not None and (ev[k] is None or v > ev[k]):
                    ev[k] = v
    # Position and context size in the thread: what a long conversation costs per message.
    turns: dict[str, int] = {}
    for mid, ev in sorted(by_id.items(), key=lambda kv: kv[1]["at"]):
        sid = sessions.get(mid)
        if sid is not None:
            ev["thread_fp"] = _hash(salt, "session\0" + sid, 16)
            turns[sid] = turns.get(sid, 0) + 1
            ev["thread_turns"] = turns[sid]
        ev["thread_tokens"] = sum(ev[k] or 0 for k in ("tokens_in", "cache_read", "cache_write"))
    return [ev for ev in by_id.values() if ev["model"]]


def import_claude_code(conn, home: Path, salt: str, person: str | None, write: bool) -> dict:
    out = {"files": 0, "skipped": 0, "added": 0, "updated": 0, "unchanged": 0, "by_model": {}}
    root = home / "projects"
    for path in sorted(root.glob("**/*.jsonl")) if root.is_dir() else []:
        try:
            st = path.stat()
        except OSError:
            continue
        pfp = _hash(salt, "path\0" + str(path.relative_to(root)))
        if file_seen(conn, pfp, st.st_size, st.st_mtime):
            out["skipped"] += 1
            continue
        events = transcript_events(path, salt, person)
        res = merge_events(conn, events, write)
        if write:
            mark_file(conn, pfp, st.st_size, st.st_mtime)
        out["files"] += 1
        for k in ("added", "updated", "unchanged"):
            out[k] += res[k]
        for ev in events:
            key = f"{ev['model']} {ev['effort'] or '-'}"
            out["by_model"][key] = out["by_model"].get(key, 0) + 1
    return out


# --- plan-usage.v1 events file ---------------------------------------------------------------------

def clean_event(raw) -> tuple[dict | None, int]:
    """(event, number of fields dropped). Only contract fields survive; strings must be up to three short
    words and counts whole numbers, so no free text can reach the database."""
    if not isinstance(raw, dict):
        return None, 0
    ev: dict = {"id": _word(raw.get("id")) if " " not in str(raw.get("id")) else None, "at": _epoch(raw.get("at"))}
    dropped = 0
    for k in _STR:
        if k in raw:
            ev[k] = _word(raw[k])
            dropped += ev[k] is None and raw[k] is not None
    for k, col in _INT.items():
        if k in raw:
            v = raw[k]
            ev[col] = v if col == "vote" and v in (-1, 0, 1) and not isinstance(v, bool) else _count(v)
            dropped += ev[col] is None and v is not None
    for k in _BOOL:
        if k in raw:
            ev[k] = 1 if raw[k] is True else 0 if raw[k] is False else None
            dropped += ev[k] is None and raw[k] is not None
    dropped += sum(1 for k in raw if k not in ("id", "at", *_STR, *_INT, *_BOOL))
    if not ev["id"] or ev["at"] is None or not ev.get("model"):
        return None, dropped
    ev.setdefault("source", "file")
    ev.setdefault("tokens_measured", 0)
    return ev, dropped


def read_events_file(path: Path) -> tuple[list[dict], int, int]:
    """(events, rejected, fields dropped) from a plan-usage.v1 file."""
    doc = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or doc.get("format") != EVENTS_FORMAT or not isinstance(doc.get("events"), list):
        raise ValueError(f'not a {EVENTS_FORMAT} file: expected {{"format": "{EVENTS_FORMAT}", "events": [...]}}')
    events, rejected, dropped = [], 0, 0
    for raw in doc["events"]:
        ev, d = clean_event(raw)
        dropped += d
        if ev is None:
            rejected += 1
        else:
            events.append(ev)
    return events, rejected, dropped


# --- meter readings --------------------------------------------------------------------------------

def _reading(at, meter, model, percent, resets_at) -> dict | None:
    at, meter = _epoch(at), _word(meter)
    if at is None or not meter or isinstance(percent, bool) or not isinstance(percent, (int, float)) \
            or not 0 <= percent <= 1000:
        return None
    return {"meter": meter, "model": _word(model) or "", "at": at, "percent": float(percent),
            "resets_at": _epoch(resets_at)}


def read_meter_file(path: Path) -> tuple[list[dict], int]:
    """(readings, rejected). Two shapes: {"format": "plan-meter.v1", "readings": [{at, meter, model,
    percent, resets_at}]}, or snapshots: {"history": [{at, limits: [{kind, model, percent, resets_at}]}],
    "at": ..., "limits": [...]} (the latest snapshot may sit at the top level)."""
    doc = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise ValueError("not a meter readings file: expected a JSON object")
    rows: list = []
    if doc.get("format") == METER_FORMAT and isinstance(doc.get("readings"), list):
        rows = [(r.get("at"), r.get("meter"), r.get("model"), r.get("percent"), r.get("resets_at"))
                if isinstance(r, dict) else None for r in doc["readings"]]
    elif isinstance(doc.get("history"), list) or isinstance(doc.get("limits"), list):
        snaps = [s for s in doc.get("history") or [] if isinstance(s, dict)]
        if isinstance(doc.get("limits"), list):
            snaps.append({"at": doc.get("at"), "limits": doc["limits"]})
        for s in snaps:
            for lim in s.get("limits") or []:
                rows.append((s.get("at"), lim.get("kind"), lim.get("model"), lim.get("percent"), lim.get("resets_at"))
                            if isinstance(lim, dict) else None)
    else:
        raise ValueError(f'not a meter readings file: expected "format": "{METER_FORMAT}" with "readings", '
                         'or snapshots under "history"')
    readings, rejected = [], 0
    for r in rows:
        x = _reading(*r) if r else None
        if x is None:
            rejected += 1
        else:
            readings.append(x)
    return readings, rejected


# --- the command -----------------------------------------------------------------------------------

def run(args, settings) -> int:
    """`trioditis plan import`. Returns the exit code."""
    files_arg, meter_arg = args.file, args.meter
    want_cc = args.claude_code
    if not (files_arg or want_cc or meter_arg is not None):  # no arguments: what the config names
        want_cc, meter_arg = settings.claude_code, settings.meter_file
        if not want_cc and not meter_arg:
            print("nothing configured: add a plan: section to the config (claude_code: true, meter_file: PATH), "
                  "or name a source: trioditis plan import FILE | --claude-code | --meter FILE")
            return 0
    if meter_arg == "":  # --meter with no file: the configured one
        meter_arg = settings.meter_file
        if not meter_arg:
            print("plan import: --meter needs a file, or plan.meter_file in the config", file=sys.stderr)
            return 2
    db_path = Path(args.db).expanduser() if args.db else DEFAULT_PATH
    write = not args.dry_run
    if write:
        db = MetricsDB(db_path)
        conn = db.conn
    else:
        db = None
        conn = open_readonly(db_path) if db_path.exists() else _empty()
    salt = load_salt(create=write)
    code = 0
    started = time.monotonic()
    try:
        with conn:
            if files_arg:
                try:
                    events, rejected, dropped = read_events_file(Path(files_arg).expanduser())
                except (OSError, ValueError) as err:
                    print(f"plan import: cannot read {files_arg}: {err}", file=sys.stderr)
                    return 2
                res = merge_events(conn, events, write)
                print(f"{files_arg}: {res['added']} added, {res['updated']} completed, {res['unchanged']} already "
                      f"there, {rejected} rejected (no id, time or model), {dropped} field(s) dropped")
            if want_cc:
                home = Path(args.claude_home).expanduser() if args.claude_home else claude_home()
                res = import_claude_code(conn, home, salt, settings.person, write)
                print(f"Claude Code: {res['files']} transcript(s) read, {res['skipped']} unchanged since the last "
                      f"import; messages {res['added']} added, {res['updated']} completed, "
                      f"{res['unchanged']} already there")
                if args.dry_run:
                    for key, n in sorted(res["by_model"].items(), key=lambda kv: -kv[1]):
                        print(f"  {key:<32} {n:>7,} messages")
            if meter_arg:
                try:
                    readings, rejected = read_meter_file(Path(meter_arg).expanduser())
                except (OSError, ValueError) as err:
                    print(f"plan import: cannot read the meter file {meter_arg}: {err}", file=sys.stderr)
                    code = 2
                else:
                    res = merge_meter(conn, readings, write)
                    print(f"meter readings: {res['added']} added, {res['unchanged']} already there, "
                          f"{rejected} rejected")
            if not write:
                conn.rollback()
    finally:
        if db is not None:
            db.close()
        else:
            conn.close()
    took = time.monotonic() - started
    print(("dry run: nothing written" if not write else f"database: {db_path}") + f" ({took:.1f} s)")
    return code


def _empty():
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn
