"""Measure how heavy each reasoning effort is on your own plan: the same prompts, run through
`claude -p` at each effort, with your subscription's meter read between batches.

    python scripts/effort_trial.py run --prompts prompts.json --out DIR
    python scripts/effort_trial.py report --out DIR
    python scripts/effort_trial.py blind --out DIR

`prompts.json` is {"prompts": [{"id": "code", "text": "..."}, ...]}. Runs go one at a time, all
prompts at one effort per batch, with a pause between batches so the meter file gets a fresh
reading. Tools are off (`--tools ""`), user and project settings are not loaded, and the system
prompt is a short neutral one, so each answer is a plain chat answer. `--bare` is never used: it
switches Claude Code to API-key billing. Answers stay in DIR (mode 600); the report keeps counts only.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trioditis_metrics.stats import ratio_interval  # noqa: E402

EFFORTS = ("low", "medium", "high", "xhigh", "max")
SYSTEM = "You are a helpful assistant. Answer the request directly and completely."


def claude_cmd(model: str, effort: str) -> list[str]:
    if effort not in EFFORTS:
        raise ValueError(f"unknown effort {effort!r}; choose from {', '.join(EFFORTS)}")
    return ["claude", "-p", "--model", model, "--effort", effort, "--tools", "", "--output-format", "json",
            "--setting-sources", "", "--system-prompt", SYSTEM]


def _write_private(path: Path, doc) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1)


def latest_meter(meter_file: Path | None) -> dict:
    """The newest reading per meter kind, {} when there is no file."""
    if not meter_file or not meter_file.exists():
        return {}
    from trioditis.plan import read_meter_file
    readings, _ = read_meter_file(meter_file)
    out: dict = {}
    for r in readings:
        key = r["meter"] + (f":{r['model']}" if r["model"] else "")
        if key not in out or r["at"] > out[key]["at"]:
            out[key] = r
    return out


def run_one(prompt: dict, model: str, effort: str, work: Path, timeout: int, runner=subprocess.run) -> dict:
    started = time.time()
    try:
        p = runner(claude_cmd(model, effort), input=prompt["text"], capture_output=True, text=True,
                   cwd=work, timeout=timeout)
        doc = json.loads(p.stdout) if p.stdout.strip() else {}
        err = None if p.returncode == 0 and not doc.get("is_error") else (p.stderr[-500:] or doc.get("subtype"))
    except subprocess.TimeoutExpired:
        doc, err = {}, f"timed out after {timeout} s"
    except ValueError as e:
        doc, err = {}, f"unreadable output: {e}"
    usage = doc.get("usage") or {}
    return {"prompt": prompt["id"], "effort": effort, "model": model, "started": started, "ended": time.time(),
            "session_id": doc.get("session_id"), "error": err, "duration_ms": doc.get("duration_ms"),
            "usage": {k: usage.get(k) for k in ("input_tokens", "cache_read_input_tokens",
                                                "cache_creation_input_tokens", "output_tokens")},
            "thinking_tokens": (usage.get("output_tokens_details") or {}).get("thinking_tokens"),
            "stop_reason": doc.get("stop_reason"), "answer": doc.get("result")}


def cmd_run(a, runner=subprocess.run, sleep=time.sleep) -> int:
    out = Path(a.out).expanduser()
    prompts = json.loads(Path(a.prompts).expanduser().read_text(encoding="utf-8"))["prompts"]
    efforts = [e.strip() for e in a.efforts.split(",") if e.strip()]
    for e in efforts:
        claude_cmd(a.model, e)  # unknown efforts fail before anything runs
    meter_file = Path(a.meter_file).expanduser() if a.meter_file else None
    print(f"{len(prompts)} prompt(s) x {len(efforts)} effort(s) on {a.model}: {len(prompts) * len(efforts)} runs")
    if a.dry_run:
        print("dry run: would run", " ".join(claude_cmd(a.model, efforts[0])).replace("--tools  ", '--tools "" '))
        return 0
    work = out / "work"
    work.mkdir(parents=True, exist_ok=True)
    batches_path = out / "batches.json"
    batches = json.loads(batches_path.read_text()) if batches_path.exists() else []
    start_meter = latest_meter(meter_file)
    base = (start_meter.get("weekly_all") or {}).get("percent")
    for i, effort in enumerate(efforts):
        if i:
            print(f"waiting {a.gap} s for a fresh meter reading", flush=True)
            sleep(a.gap)
        before = latest_meter(meter_file)
        now = (before.get("weekly_all") or {}).get("percent")
        if base is not None and now is not None and now - base > a.max_weekly_rise:
            print(f"stopped: the weekly meter rose {now - base:g} points since the start "
                  f"(limit {a.max_weekly_rise:g}); {effort} and later not run")
            _write_private(batches_path, batches)
            return 3
        batch = {"effort": effort, "start": time.time(), "meter_before": before}
        for p in prompts:
            path = out / "runs" / f"{effort}-{p['id']}.json"
            if path.exists() and not json.loads(path.read_text()).get("error"):
                print(f"  {effort:<6} {p['id']:<10} already done")
                continue
            r = run_one(p, a.model, effort, work, a.timeout, runner)
            _write_private(path, r)
            print(f"  {effort:<6} {p['id']:<10} out {r['usage']['output_tokens'] or 0:>7,} tokens, "
                  f"{(r['ended'] - r['started']):5.0f} s" + (f"  ERROR {r['error']}" if r["error"] else ""), flush=True)
        batch["end"] = time.time()
        batches = [b for b in batches if b["effort"] != effort] + [batch]
        _write_private(batches_path, batches)
    print(f"waiting {a.gap} s for the closing meter reading", flush=True)
    sleep(a.gap)
    batches[-1]["meter_after"] = latest_meter(meter_file)
    _write_private(batches_path, batches)
    print(f"done: {out}")
    return 0


def load_runs(out: Path) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted((out / "runs").glob("*.json"))]


def ledger_check(runs: list[dict], conn, salt: str) -> dict:
    """Each run's output tokens against the plan-usage ledger (`trioditis plan import --claude-code`),
    matched by the salted session id and the run's model; Claude Code's small side calls on another
    model are left out. Read-only."""
    from trioditis.plan import _hash
    out: dict = {"matched": 0, "missing": [], "differ": []}
    for r in runs:
        if r.get("error") or not r.get("session_id") or r["usage"].get("output_tokens") is None:
            continue
        n, tok = conn.execute("SELECT COUNT(*), SUM(tokens_out) FROM plan_usage WHERE thread_fp = ? AND model = ?",
                              (_hash(salt, "session\0" + r["session_id"], 16), r["model"])).fetchone()
        if not n:
            out["missing"].append(f"{r['effort']}-{r['prompt']}")
        elif tok != r["usage"]["output_tokens"]:
            out["differ"].append({"run": f"{r['effort']}-{r['prompt']}", "run_out": r["usage"]["output_tokens"],
                                  "ledger_out": tok})
        else:
            out["matched"] += 1
    return out


def summarise(runs: list[dict], batches: list[dict], top: str = "max") -> dict:
    ok = [r for r in runs if not r.get("error") and r["usage"].get("output_tokens") is not None]
    efforts = [e for e in EFFORTS if any(r["effort"] == e for r in ok)]
    prompts = sorted({r["prompt"] for r in ok})
    cell = {(r["prompt"], r["effort"]): r for r in ok}
    table = {p: {e: cell[(p, e)]["usage"]["output_tokens"] for e in efforts if (p, e) in cell} for p in prompts}
    ratios = {}
    for e in efforts:
        if e == top:
            continue
        r = ratio_interval([(table[p].get(top), table[p].get(e)) for p in prompts
                            if top in table[p] and e in table[p]])
        if r:
            ratios[f"{top}/{e}"] = r
    totals = {e: {"runs": sum(1 for r in ok if r["effort"] == e),
                  "output_tokens": sum(r["usage"]["output_tokens"] for r in ok if r["effort"] == e),
                  "thinking_tokens": sum(r.get("thinking_tokens") or 0 for r in ok if r["effort"] == e)}
              for e in efforts}
    meter = []
    for i, b in enumerate(batches):
        after = batches[i + 1]["meter_before"] if i + 1 < len(batches) else b.get("meter_after") or {}
        row = {"effort": b["effort"], "minutes": round((b["end"] - b["start"]) / 60, 1)}
        for k in ("session", "weekly_all"):
            x, y = (b["meter_before"].get(k) or {}).get("percent"), (after.get(k) or {}).get("percent")
            row[k] = None if x is None or y is None else y - x
        # A reading older than the previous batch's end blurs the split between the two batches.
        at = (b["meter_before"].get("session") or {}).get("at")
        row["reading_age_min"] = None if at is None else round((b["start"] - at) / 60, 1)
        row["stale"] = bool(at is not None and i and at < batches[i - 1]["end"])
        meter.append(row)
    return {"prompts": prompts, "efforts": efforts, "output_tokens": table, "ratios": ratios, "totals": totals,
            "meter": meter, "errors": [(r["prompt"], r["effort"], r["error"]) for r in runs if r.get("error")]}


def cmd_report(a) -> int:
    out = Path(a.out).expanduser()
    batches_path = out / "batches.json"
    runs = load_runs(out)
    s = summarise(runs, json.loads(batches_path.read_text()) if batches_path.exists() else [])
    s["ledger"] = None
    if not a.no_ledger:
        from trioditis.plan import load_salt
        from trioditis_metrics.db import DEFAULT_PATH, open_readonly
        db = Path(a.db).expanduser() if a.db else DEFAULT_PATH
        salt = load_salt(create=False)
        if db.exists() and salt != "dry-run":
            conn = open_readonly(db)
            try:
                s["ledger"] = ledger_check(runs, conn, salt)
            finally:
                conn.close()
    (out / "report.json").write_text(json.dumps(s, indent=1))
    if a.json:
        print(json.dumps(s, indent=1))
        return 0
    print("output tokens per prompt (thinking included)")
    print(f"{'prompt':<10}" + "".join(f"{e:>9}" for e in s["efforts"]))
    for p in s["prompts"]:
        print(f"{p:<10}" + "".join(f"{s['output_tokens'][p].get(e, 0):>9,}" for e in s["efforts"]))
    for k, r in s["ratios"].items():
        ci = f"95% CI {r['lo']:.2f}-{r['hi']:.2f}" if r["lo"] else "no interval (one prompt)"
        print(f"{k:<12} {r['ratio']:.2f}x  ({ci}, n={r['n']})")
    print("meter rise per batch (whole percents, shared with anything else using the plan)")
    for m in s["meter"]:
        print(f"  {m['effort']:<6} {m['minutes']:>5} min  session {m['session']}  weekly {m['weekly_all']}"
              + (f"  (reading {m['reading_age_min']} min old at start, before the last batch ended)"
                 if m.get("stale") else ""))
    lg = s["ledger"]
    if lg is None:
        print("ledger: not checked (no database or salt; run `trioditis plan import --claude-code` first)"
              if not a.no_ledger else "ledger: not checked (--no-ledger)")
    else:
        print(f"ledger: {lg['matched']} run(s) match the plan-usage ledger"
              + (f"; not in it yet: {', '.join(lg['missing'])} (run `trioditis plan import --claude-code`)"
                 if lg["missing"] else "")
              + "".join(f"; {d['run']} differs: run {d['run_out']:,}, ledger {d['ledger_out']:,}" for d in lg["differ"]))
    for e in s["errors"]:
        print("error:", *e)
    return 0


def cmd_blind(a) -> int:
    """Answers per prompt under shuffled letters; the key stays in DIR/key.json."""
    out = Path(a.out).expanduser()
    runs = [r for r in load_runs(out) if not r.get("error") and r.get("answer")]
    prompts = {p["id"]: p for p in json.loads(Path(a.prompts).expanduser().read_text())["prompts"]}
    # Unseeded, the order comes from the OS so nobody can rebuild the key; --seed is for tests.
    rng = random.SystemRandom() if a.seed is None else random.Random(a.seed)  # noqa: S311
    items, key = [], {}
    for pid in sorted({r["prompt"] for r in runs}):
        group = [r for r in runs if r["prompt"] == pid]
        rng.shuffle(group)
        letters = "ABCDEFGH"[:len(group)]
        key[pid] = {letters[i]: r["effort"] for i, r in enumerate(group)}
        items.append({"id": pid, "prompt": prompts[pid]["text"],
                      "answers": [{"letter": letters[i], "text": r["answer"]} for i, r in enumerate(group)]})
    _write_private(out / "key.json", key)
    _write_private(out / "blind.json", {"items": items})
    print(f"blind.json: {len(items)} prompt(s); key.json holds the letters")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run every prompt at every effort")
    r.add_argument("--prompts", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--efforts", default="medium,high,xhigh,max")
    r.add_argument("--model", default="claude-opus-5-5")
    r.add_argument("--meter-file", help="plan meter readings (snapshots or plan-meter.v1)")
    r.add_argument("--gap", type=int, default=360, help="seconds between batches (default 360)")
    r.add_argument("--max-weekly-rise", type=float, default=3.0, help="stop above this many points (default 3)")
    r.add_argument("--timeout", type=int, default=900, help="seconds per run (default 900)")
    r.add_argument("--dry-run", action="store_true")
    rp = sub.add_parser("report", help="tokens, ratios with 95%% intervals, meter rise")
    rp.add_argument("--out", required=True)
    rp.add_argument("--json", action="store_true")
    rp.add_argument("--db", help="the metrics database with the plan-usage ledger (default: Trioditis's)")
    rp.add_argument("--no-ledger", action="store_true", help="skip the check against the ledger")
    b = sub.add_parser("blind", help="shuffled answers for rating; the key stays local")
    b.add_argument("--out", required=True)
    b.add_argument("--prompts", required=True)
    b.add_argument("--seed", type=int, default=None)
    a = ap.parse_args(argv)
    return {"run": cmd_run, "report": cmd_report, "blind": cmd_blind}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
