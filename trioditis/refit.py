"""Daily refit: Trioditis's own numbers, each with how sure it is, and what's new about its models.

Reads the metrics database (read-only), the config, prices.json and, when the config's `facts:`
names one, a provider-facts file. Writes:

- ~/.hlidskjalv/trioditis-fitted.json: per model, its error, cut-off, asked-again and cache-hit
  rates with 95% intervals, p50/p95 latency and a daily error chart; what the facts say about the
  configured models; price changes waiting to be applied; with a plan-usage ledger, meter points
  per token for each model and effort (planfit.py). Every number carries its n, its window and
  the method behind it.
- ~/.hlidskjalv/trioditis-notebook.jsonl: one line per finding that wasn't there at the last refit
  (a drift alarm, a lifecycle warning, a price change, a model to try).

It changes nothing the router uses: prices change when you run `prices import`, models and
routes when you edit the config.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import sqlite3
import sys
import time
from pathlib import Path

from trioditis_metrics.db import DEFAULT_PATH, daily_outcomes, latencies, open_readonly, outcome_rates, usage
from trioditis_metrics.home import data_home
from trioditis_metrics.prices import DEFAULT_PRICES_PATH
from trioditis_metrics.stats import ewma_chart, quantile

from .config import Config
from .planfit import METHOD as PLAN_METHOD
from .planfit import fit_plan, summary_lines
from .pricefacts import facts_by_api_id, lifecycle_lines, lifecycle_report, same_price, stale_warning

SCHEMA = "trioditis-fitted.v1"
MIN_DAY_CALLS = 10  # a day with fewer calls says too little about its error rate to chart
WARMUP_DAYS = 10    # days that set the chart's baseline before it can raise an alarm
METHODS = {
    "rates": "errors: share of calls; cut off: share of answers with a finish reason; asked again: share of "
             "fingerprinted answers repeated within 2 minutes. 95% Wilson score interval",
    "cache_hit": "cached prompt tokens / prompt tokens of calls whose provider reports caching; no interval",
    "latency_ms": "p50 and p95 of successful calls, linear interpolation",
    "daily_errors": f"EWMA control chart (alpha 0.2, L 3) of the daily error rate over days with at least "
                    f"{MIN_DAY_CALLS} calls; target and binomial spread from the first {WARMUP_DAYS} such days",
    "plan_weights": PLAN_METHOD,
    "plan_signals": "share of sends with each result signal recorded (suggestion followed, answer regenerated, asked "
                    "again, up vote among votes), per model and effort; 95% Wilson score interval",
}


def fitted_path() -> Path:
    return data_home() / "trioditis-fitted.json"


def notebook_path() -> Path:
    return data_home() / "trioditis-notebook.jsonl"


def fit(conn: sqlite3.Connection, cfg: Config, facts: dict | None, prices: dict, now: float,
        days: int = 28, today: dt.date | None = None) -> dict:
    since = now - days * 86400
    lat = latencies(conn, since, now)
    series: dict[str, list[dict]] = {}
    for r in daily_outcomes(conn, since, now):
        series.setdefault(r["model"], []).append(r)
    models = {}
    for r in usage(conn, since, now, "model"):
        ms = lat.get(r["key"], [])
        models[r["key"]] = {
            "calls": r["calls"],
            "rates": outcome_rates(r),
            "latency_ms": {"p50": quantile(ms, 0.5), "p95": quantile(ms, 0.95), "n": len(ms)},
            "daily_errors": _chart(series.get(r["key"], [])),
        }
    doc = {"$schema": SCHEMA, "fitted_at": _iso(now),
           "window": {"since": _iso(since), "until": _iso(now), "days": days},
           "methods": METHODS, "models": models, "plan": fit_plan(conn, since, now),
           "facts": None, "lifecycle": None, "proposals": []}
    if facts is not None:
        cloud = {n: m.upstream_id for n, m in cfg.models.items() if not cfg.providers[m.provider].local}
        doc["facts"] = {"path": cfg.facts, "generated": facts.get("generatedAt"),
                        "stale": stale_warning(facts, today)}
        doc["lifecycle"] = lifecycle_report(facts, cloud, today)
        known = facts_by_api_id(facts)
        for name, upstream_id in sorted(cloud.items()):
            new, old = known.get(upstream_id), prices.get(name)
            if new and not same_price(old if isinstance(old, dict) else None, new):
                doc["proposals"].append({
                    "kind": "price", "model": name, "api_id": upstream_id,
                    "from": {k: old.get(k) for k in ("input", "output")} if isinstance(old, dict) else None,
                    "to": {k: new[k] for k in ("input", "output")}, "source": new["_source"],
                    "fetched": new["_fetched"], "apply": f"trioditis prices import {cfg.facts}"})
    return doc


def _chart(days: list[dict]) -> dict:
    usable = [d for d in days if d["calls"] >= MIN_DAY_CALLS]
    if len(usable) <= WARMUP_DAYS:
        return {"status": "not enough data", "days_with_data": len(usable),
                "needs": f"more than {WARMUP_DAYS} days with at least {MIN_DAY_CALLS} calls"}
    base, rest = usable[:WARMUP_DAYS], usable[WARMUP_DAYS:]
    calls = sum(d["calls"] for d in base)
    # Target with half an error added, so a clean baseline still has a spread to judge against;
    # spread of one day's rate from the binomial, at the baseline's mean day size.
    mu0 = (sum(d["errors"] for d in base) + 0.5) / (calls + 1)
    sigma = math.sqrt(mu0 * (1 - mu0) / (calls / len(base)))
    chart = ewma_chart([d["errors"] / d["calls"] for d in rest], mu0=mu0, sigma=sigma)
    last = chart["points"][-1]
    return {"status": "out of limits" if last["out"] else "in control", "days_with_data": len(usable),
            "baseline": mu0, "sigma": sigma, "alarms": [rest[i]["day"] for i in chart["alarms"]],
            "last": {"day": rest[-1]["day"], "ewma": last["ewma"], "lo": last["lo"], "hi": last["hi"]}}


def findings(doc: dict) -> list[dict]:
    """Everything in a fitted document worth a line in the notebook, each with a stable key."""
    out = []
    for name, m in doc["models"].items():
        for day in m["daily_errors"].get("alarms", []):
            out.append({"key": f"drift:{name}:{day}", "kind": "drift", "subject": name, "decision": "alert",
                        "finding": f"{name}: the daily error rate left its control limits on {day}"})
    for meter, m in ((doc.get("plan") or {}).get("meters") or {}).items():
        for g, e in m["groups"].items():
            if e["status"] == "measured":
                w = e["per_mtok"]
                ci = "" if w["lo"] is None else f" (95% {w['lo']:.3g}-{w['hi']:.3g})"
                out.append({"key": f"plan-weight:{meter}:{g}:{e['basis']}", "kind": "plan-weight", "subject": g,
                            "decision": "proposal",
                            "finding": f"{g} on the {meter} meter: {w['value']:.3g} points per 1M {e['basis']} "
                                       f"tokens{ci}, from {e['stretches']} quiet stretches; review it before it "
                                       f"goes into the team rules file"})
    if doc.get("facts") and doc["facts"].get("stale"):
        out.append({"key": f"stale:{doc['facts']['generated']}", "kind": "facts", "subject": doc["facts"]["path"],
                    "decision": "alert", "finding": f"model facts: {doc['facts']['stale']}"})
    lc = doc.get("lifecycle") or {"warnings": [], "unknown": [], "challengers": []}
    for w, line in zip(lc["warnings"], lifecycle_lines({**lc, "unknown": []}), strict=True):
        out.append({"key": f"lifecycle:{w['model']}:{w['why']}", "kind": "lifecycle", "subject": w["model"],
                    "decision": "alert", "finding": line})
    for u in lc["unknown"]:
        out.append({"key": f"unknown:{u['model']}:{u['api_id']}", "kind": "lifecycle", "subject": u["model"],
                    "decision": "info",
                    "finding": f"{u['model']} ({u['api_id']}): not in the facts file, so its status is unknown"})
    for c in lc["challengers"]:
        price = f" at ${c['price']['input']}/${c['price']['output']} per 1M tokens" if c["price"] else ""
        out.append({"key": f"challenger:{c['api_id']}", "kind": "challenger", "subject": c["api_id"],
                    "decision": "info",
                    "finding": f"{c['api_id']} ({c['provider']}) is active{price} and not in your config: "
                               f"a candidate to test before adding"})
    for p in doc["proposals"]:
        to = p["to"]
        out.append({"key": f"price:{p['model']}:{to['input']}/{to['output']}", "kind": "price", "subject": p["model"],
                    "decision": "proposal",
                    "finding": f"{p['model']} ({p['api_id']}): the facts say ${to['input']}/${to['output']} per 1M "
                               f"tokens, prices.json says "
                               + (f"${p['from']['input']}/${p['from']['output']}" if p["from"] else "nothing")
                               + f"; apply with: {p['apply']}"})
    return out


def new_findings(previous: dict | None, doc: dict) -> list[dict]:
    seen = {f["key"] for f in findings(previous)} if previous else set()
    return [f for f in findings(doc) if f["key"] not in seen]


def run(cfg: Config, args) -> int:
    db = Path(args.db).expanduser() if args.db else DEFAULT_PATH
    out = Path(args.out).expanduser() if args.out else fitted_path()
    book = Path(args.notebook).expanduser() if args.notebook else notebook_path()
    facts = None
    if cfg.facts:
        try:
            facts = json.loads(Path(cfg.facts).read_text(encoding="utf-8"))
            facts_by_api_id(facts)  # the schema check, before anything relies on it
        except (OSError, ValueError) as err:
            print(f"refit: cannot use the facts file {cfg.facts}: {err}; fitting outcomes only", file=sys.stderr)
            facts = None
    prices = _read_json(Path(args.prices).expanduser() if args.prices else DEFAULT_PRICES_PATH) or {}
    try:
        conn = open_readonly(db)
    except sqlite3.Error:
        print(f"refit: no metrics database at {db} yet; nothing to fit")
        return 0
    try:
        doc = fit(conn, cfg, facts, prices, time.time(), args.days)
    finally:
        conn.close()
    new = new_findings(_read_json(out), doc)
    if args.json:
        print(json.dumps(doc, indent=2))
    else:
        _print_summary(doc, new)
    if args.dry_run:
        print(f"dry run: {out} and {book} not changed")
        return 0
    _write_private(out, json.dumps(doc, indent=2) + "\n")
    if new:
        lines = "".join(json.dumps({"ts": doc["fitted_at"], "system": "trioditis",
                                    **{k: v for k, v in f.items() if k != "key"}, "key": f["key"]}) + "\n"
                        for f in new)
        _append_private(book, lines)
    if not args.json:
        print(f"wrote {out}" + (f"; {len(new)} new finding(s) in {book}" if new else "; nothing new for the notebook"))
    return 0


def _print_summary(doc: dict, new: list[dict]) -> None:
    w = doc["window"]
    total = sum(m["calls"] for m in doc["models"].values())
    print(f"refit over {w['days']} days to {w['until']}: {total} calls")
    for name, m in doc["models"].items():
        err = m["rates"]["error"]
        lat = m["latency_ms"]
        chart = m["daily_errors"]
        line = f"  {name}: {m['calls']} calls, errors {err['value']:.0%} ({err['lo']:.0%}-{err['hi']:.0%})"
        if lat["p50"] is not None:
            line += f", latency p50 {lat['p50']:,.0f} ms, p95 {lat['p95']:,.0f} ms"
        line += f"; daily chart: {chart['status']}"
        if chart["status"] == "not enough data":
            line += f" ({chart['days_with_data']} so far; needs {chart['needs']})"
        print(line)
    for line in summary_lines(doc.get("plan")):
        print(line)
    for f in findings(doc):
        if f["kind"] not in ("drift", "plan-weight"):
            print(f"  {'new: ' if f in new else ''}{f['finding']}")


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts))


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _append_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(text)
