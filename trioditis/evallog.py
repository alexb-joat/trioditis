"""`trioditis eval-routes --log`: the route picker against the router, on your own traffic.

With a `route_picker:` block in the config, every classified request logs the route that was
served, the route the router alone would have picked, and what the picker did. This reads
that decision log, and the shadow counts beside it (`shadow.jsonl`: Claude Code's first turns, classified
but never served, trioditis/shadow.py), and counts how often the picker answered, how often the two disagreed and
where, and estimates what the disagreements cost. It reads files only: no network call, nothing written.

The log carries no labels, so this measures disagreement, never which of the two was right.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from trioditis_metrics import Prices
from trioditis_metrics.stats import quantile

from .config import Config
from .evalroutes import _fit, _pct

ASKED = ("chose", "timeout", "no_route", "error")   # the picker was called
NOT_ASKED = {"skipped": "tools matched a route, no text, or the prompt was over max_chars",
             "busy": "another picker call was still running",
             "not_serving": "an adopted setting had the router serve (trioditis reinforce)"}


class LogError(Exception):
    """The decision log can't be read; the message says what to do."""


@dataclass
class LogData:
    files: list[str]
    lines: int = 0            # log lines that parsed as a decision
    unreadable: int = 0       # lines that are not a JSON object
    records: list[dict] = field(default_factory=list)   # decisions classified with the picker on
    served: dict[str, Counter] = field(default_factory=dict)   # route -> (model, local) of every answered request
    shadow: int = 0           # of the records, shadow counts (classified, never served)


def read_log(path: str | Path) -> LogData:
    """Decisions from `path`, and from `<path>.1` first when the log has rotated."""
    p = Path(path).expanduser()
    if p.is_dir():
        raise LogError(f"{p} is a directory; name the decision log itself (a .jsonl file)")
    older = p.with_name(p.name + ".1")
    paths = [q for q in (older, p) if q.is_file()]
    if p not in paths:
        raise LogError(f"no decision log at {p}; the router writes it at `logging.path` once it has served a "
                       "request, or name the file after --log")
    data = LogData(files=[str(q) for q in paths])
    for q in paths:
        try:
            text = q.read_text(encoding="utf-8", errors="replace")
        except OSError as err:
            raise LogError(f"can't read {q}: {err.strerror or err}") from err
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                rec = None
            if not isinstance(rec, dict):
                data.unreadable += 1
                continue
            data.lines += 1
            status = rec.get("status")
            if isinstance(rec.get("route"), str) and isinstance(rec.get("model"), str) and isinstance(status, int) \
                    and status < 400:
                data.served.setdefault(rec["route"], Counter())[(rec["model"], bool(rec.get("local")))] += 1
            picker = rec.get("route_picker")
            if isinstance(picker, dict) and isinstance(rec.get("router_route"), str):
                data.records.append(rec)
                if rec.get("shadow") is True:  # `--log shadow.jsonl` names the shadow log itself
                    data.shadow += 1
    return data


def read_shadow(data: LogData, decisions_path: str | Path) -> None:
    """Add the shadow counts beside the decision log (`shadow.jsonl`, and `.1` first), when there are any
    and `read_log` has not read them already."""
    p = Path(decisions_path).expanduser().with_name("shadow.jsonl")
    done = {Path(f).resolve() for f in data.files}
    for q in (p.with_name(p.name + ".1"), p):
        if not q.is_file() or q.resolve() in done:
            continue
        try:
            text = q.read_text(encoding="utf-8", errors="replace")
        except OSError as err:
            raise LogError(f"can't read {q}: {err.strerror or err}") from err
        data.files.append(str(q))
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                rec = None
            if not isinstance(rec, dict):
                data.unreadable += 1
            elif rec.get("shadow") is True and isinstance(rec.get("route_picker"), dict) \
                    and isinstance(rec.get("router_route"), str):
                data.records.append(rec)
                data.shadow += 1


@dataclass
class Cost:
    n: int = 0                # disagreements
    priced: int = 0           # of them, with a price for both sides
    served: float = 0.0       # USD, as served (the picker's route)
    router: float = 0.0       # USD, the same token counts on the model the router's route usually serves


@dataclass
class Summary:
    n: int
    outcomes: Counter
    pickers: Counter          # (model, instruction version) -> records
    ms: list[int]             # time to answer, where the picker chose
    agreed: int
    pairs: Counter            # (router's route, picker's route) -> count, disagreements only
    cost: Cost
    first: str
    last: str

    @property
    def chose(self) -> int:
        return self.outcomes["chose"]

    @property
    def asked(self) -> int:
        return sum(self.outcomes[o] for o in ASKED)


def _router_side(cfg: Config, route: str, served: dict[str, Counter]) -> tuple[str, bool] | None:
    """(model, whether it is local) the router's route would have used: the model that route most often
    answered with in this log (so `prefer: cheapest` and fallbacks count as they happened), else its first
    configured model. None when today's config no longer has the route."""
    r = cfg.routes.get(route)
    if r is None or not r.models:
        return None
    if served.get(route):
        return served[route].most_common(1)[0][0]
    m = cfg.models.get(r.models[0])
    prov = cfg.providers.get(m.provider) if m else None
    return (m.name, bool(prov.local)) if m and prov else None


def summarise(cfg: Config, records: list[dict], prices: Prices,
              answered: dict[str, Counter] | None = None) -> Summary:
    outcomes: Counter = Counter()
    pickers: Counter = Counter()
    pairs: Counter = Counter()
    ms: list[int] = []
    agreed = 0
    cost = Cost()
    days = sorted(str(r.get("ts", ""))[:10] for r in records if r.get("ts"))
    for rec in records:
        info = rec["route_picker"]
        outcome = str(info.get("outcome") or "unknown")
        outcomes[outcome] += 1
        pickers[(str(info.get("model")), str(info.get("instruction")))] += 1
        if outcome != "chose":
            continue
        if isinstance(info.get("ms"), int):
            ms.append(info["ms"])
        router, picked = rec["router_route"], info.get("route")
        if picked == router:
            agreed += 1
            continue
        pairs[(router, str(picked))] += 1
        if rec.get("shadow") is True:
            continue  # nothing was served, so there is no cost to compare
        cost.n += 1
        side = _router_side(cfg, router, answered or {})
        status, usage = rec.get("status"), rec.get("usage")
        if side is None or not isinstance(status, int) or status >= 400:
            continue
        served = prices.cost(rec.get("model"), rec.get("local"), usage)
        other = prices.cost(side[0], side[1], usage)
        if served is None or other is None:
            continue
        cost.priced += 1
        cost.served += served
        cost.router += other
    return Summary(n=len(records), outcomes=outcomes, pickers=pickers, ms=ms, agreed=agreed, pairs=pairs,
                   cost=cost, first=days[0] if days else "", last=days[-1] if days else "")


def _usd(x: float) -> str:
    return f"${x:,.4f}"


def format_report(data: LogData, s: Summary, config_path: str, today: str, picker_on: bool) -> list[str]:
    span = f"{s.first} to {s.last}" if s.first else "no dates"
    out = ["Route picker against the router, from the decision log",
           f"  date     {today}",
           f"  log      {' + '.join(data.files)}",
           f"  config   {config_path}",
           f"  {data.lines:,} logged requests; {s.n:,} were classified with the picker on ({span})."
           if s.n else f"  {data.lines:,} logged requests; none was classified with the picker on."]
    if data.shadow:
        out.append(f"  {data.shadow:,} of the {s.n:,} are shadow counts: Claude Code first turns through the gate, "
                   "classified by both but never served, so they carry no cost.")
    if data.unreadable:
        out.append(f"  {data.unreadable:,} lines were not valid JSON and were skipped.")
    if not s.n:
        out += ["", "Nothing to count yet." + (
            " The config has a `route_picker:` block, so first turns of `auto` conversations will be logged "
            "with both routes from the next router start." if picker_on else
            " Add a `route_picker:` block to the config (config.example.yaml shows one), restart the router "
            "and run this again after some `auto` requests.")]
        return out
    out.append("  The rest were pinned follow-up turns, forced routes, explicit models, or logged with the picker off.")
    for (model, instruction), k in s.pickers.most_common():
        out.append(f"  picker   {model}, instruction {instruction} ({k:,} requests)")
    if len(s.pickers) > 1:
        out.append("  More than one picker model or instruction: the counts below pool them.")

    out += ["", f"What the picker did (n = {s.n:,})"]
    width = max(len(o) for o in s.outcomes)
    for outcome, k in s.outcomes.most_common():
        why = f"   ({NOT_ASKED[outcome]})" if outcome in NOT_ASKED else ""
        out.append(f"  {outcome:<{width}}  {k:>6,}{why}")
    out.append(f"  Asked {s.asked:,} times, named a route: {_pct(s.chose, s.asked)}")
    if s.ms:
        out.append(f"  Time to answer: median {quantile(s.ms, 0.5) / 1000:.2f} s, 95th percentile "
                   f"{quantile(s.ms, 0.95) / 1000:.2f} s (n = {len(s.ms):,})")
    if not s.chose:
        out += ["", "The picker never named a route, so there is no disagreement to count. "
                    "`trioditis check` says whether it answers."]
        return out

    out += ["", f"Where the picker chose (n = {s.chose:,})",
            f"  same route as the router: {_pct(s.agreed, s.chose)}"]
    if s.pairs:
        rows = [(f"{a} -> {b}", k) for (a, b), k in s.pairs.most_common()]
        width = max(len("router -> picker"), *(len(r[0]) for r in rows))
        out.append(f"  {'router -> picker':<{width}}  {'n':>6}")
        out += [f"  {name:<{width}}  {k:>6,}" for name, k in rows]
    out.append("  The log has no labels: this counts disagreement, not which of the two was right.")

    c = s.cost
    if c.n:
        out += ["", f"What the {c.n:,} disagreements cost (an estimate)"]
        if c.priced:
            diff = c.served - c.router
            out.append(f"  {c.priced:,} of {c.n:,} could be priced. As served: {_usd(c.served)}. The same token counts on the "
                       f"model the router's route usually answered with: {_usd(c.router)}. Difference: {'+' if diff >= 0 else '-'}{_usd(abs(diff))}.")
            out.append("  \"Usually\" is the model that route most often answered with in this log, or its first "
                       "configured model when the log has none. Another model would have written a different number "
                       "of tokens, so read the difference as a direction and a rough size.")
        else:
            out.append("  None could be priced.")
        if c.priced < c.n:
            out.append(f"  {c.n - c.priced:,} had no price: the call failed, logged no token counts, used a model "
                       "missing from prices.json, or the router's route is no longer in this config.")
    return out


def run(cfg: Config, args, *, prices: Prices | None = None, today: str = "") -> int:
    path = args.log or cfg.logging.path
    if not path:
        sys.exit("eval-routes: the config sets `logging.path` to nothing, so no decision log exists; "
                 "name a file after --log, or set logging.path and restart the router")
    try:
        data = read_log(path)
        read_shadow(data, path)
    except LogError as err:
        sys.exit(f"eval-routes: {err}")
    s = summarise(cfg, data.records, prices or Prices(), data.served)
    lines = format_report(data, s, str(getattr(args, "config", "")), today or dt.date.today().isoformat(),
                          cfg.route_picker is not None)
    print("\n".join(w for line in lines for w in _fit(line)).rstrip())
    return 0
