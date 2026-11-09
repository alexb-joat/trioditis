"""Plan weights: how many meter points each model and effort costs, fitted from the plan-usage ledger.

A plan meter shows whole percents, and everyone using the account (other people, the chat app,
another computer) raises it, while the ledger only holds what was tracked here. So a weight can
only come from stretches where the rise is plausibly ours:

1. Steps: the intervals between consecutive readings of a meter. A step that spans a reset, or is
   longer than MAX_STEP_S (the reader was off), is skipped with whatever was sent in it.
2. Stretches: runs of consecutive steps in which one model and effort sent at least PURITY of the
   tracked tokens, by every token measure. One idle step right after a run joins it, because the
   meter can show the last messages one reading late. Summing a run's steps cancels the rounding
   inside it, so a stretch is off by under one point, however long it is.
3. Quiet: a stretch counts only when the meter stood still in every idle step within QUIET_S
   before and after it, and there was at least one. Untracked use during a stretch can't be seen;
   this keeps out the times someone else was clearly using the account.
4. Weight: points per million tokens, sum of rises over sum of tokens across the quiet stretches
   (stats.ratio_estimate), with n and a 95% interval. The token measure is chosen per meter: the
   one under which each group's stretches agree best (smallest residual share).

Nothing here is applied: a measured weight is a proposal for the team rules file, after review.
"""

from __future__ import annotations

import math
import time
from collections import defaultdict

from trioditis_metrics.plan import NEW_WINDOW_S, has_tables, meter_readings, plan_messages, signal_counts
from trioditis_metrics.stats import ratio_estimate, t95, wilson

BASES = ("uncached", "output", "all")  # order breaks ties
PURITY = 0.9          # share of a step's tracked tokens one group must have sent, by every measure
MAX_STEP_S = 900      # a longer step between readings means the reader was off
QUIET_S = 1800        # idle steps this close to a stretch must show no rise
MIN_STRETCHES = 3
MIN_POINTS = 10       # below this, rounding alone is over a tenth of the total
ROUNDING_VAR = 1 / 6  # a stretch's rise is the difference of two readings each rounded down: 2 x 1/12

METHOD = (f"plan meter points per 1M tokens per model and effort, from stretches of consecutive readings in which one "
          f"model and effort sent at least {PURITY:.0%} of the tracked tokens (plus one idle reading for meter lag), "
          f"counted only when the meter stood still in the idle readings within {QUIET_S // 60} minutes either side; "
          f"sum of rises over sum of tokens, 95% t-interval of the ratio estimator (Cochran 6.3) with each stretch's "
          f"variance at least the 1/6 point² of rounding; relative weights against the group with most stretches, "
          f"interval on the log scale. Token measure per meter: the one with the smallest residual share. Needs "
          f"{MIN_STRETCHES} stretches and {MIN_POINTS} points")
CAVEATS = (
    "The meter shows whole percents and counts everyone on the account; the ledger holds only tracked use. Only "
    "quiet stretches with tracked use count, so untracked use during one would make a weight look heavier.",
    "A message counts at its first timestamp; a long answer can raise the meter over the next readings, and its "
    "stretch then fails the quiet test.",
    "Per message means per assistant message in the ledger: one Claude Code turn with tools is several messages.",
    "Measured weights are proposals: review them before they go into the team rules file. Nothing is applied.",
)


def _tokens(m: dict) -> dict:
    uncached = m["tokens_in"] + m["tokens_out"] + m["cache_write"]
    return {"uncached": uncached, "output": m["tokens_out"], "all": uncached + m["cache_read"]}


def steps(readings: list[tuple], messages: list[dict]) -> list[dict]:
    """The steps between consecutive readings, each with its kind (active, idle, gap, reset), its rise
    and the tracked tokens per model and effort."""
    out, k = [], 0
    for (a, pa, ra), (b, pb, rb) in zip(readings, readings[1:], strict=False):
        groups: dict[str, dict] = {}
        while k < len(messages) and messages[k]["at"] <= b:
            m = messages[k]
            k += 1
            if m["at"] <= a:
                continue
            g = groups.setdefault(f"{m['model']} {m['effort'] or '-'}", {"messages": 0, **dict.fromkeys(BASES, 0)})
            g["messages"] += 1
            for basis, v in _tokens(m).items():
                g[basis] += v
        moved = ra is not None and rb is not None and abs(rb - ra) > NEW_WINDOW_S
        kind = ("reset" if pb < pa or moved else "gap" if b - a > MAX_STEP_S else "active" if groups else "idle")
        out.append({"start": a, "end": b, "rise": pb - pa if kind != "reset" else None, "kind": kind,
                    "groups": groups})
    return out


def _owner(step: dict) -> str | None:
    if step["kind"] != "active":
        return None
    for name, g in step["groups"].items():
        if all(g[b] >= PURITY * total for b in BASES
               if (total := sum(x[b] for x in step["groups"].values())) > 0):
            return name
    return None


def stretches(st: list[dict]) -> list[dict]:
    out: list[dict] = []
    cur = None
    for i, s in enumerate(st):
        owner = _owner(s)
        follows = cur is not None and cur["last"] == i - 1 and not cur["tail"]
        if owner is not None:
            if not (follows and cur["group"] == owner):
                cur = {"group": owner, "first": i, "last": i, "tail": False, "rise": 0.0, "messages": 0,
                       **dict.fromkeys(BASES, 0)}
                out.append(cur)
            g = s["groups"][owner]
            cur["last"], cur["rise"], cur["messages"] = i, cur["rise"] + s["rise"], cur["messages"] + g["messages"]
            for b in BASES:
                cur[b] += g[b]
        elif s["kind"] == "idle" and follows:
            cur.update(last=i, tail=True, rise=cur["rise"] + s["rise"])
    return out


def quiet(st: list[dict], stretch: dict) -> bool:
    start, end = st[stretch["first"]]["start"], st[stretch["last"]]["end"]
    near = []
    i = stretch["first"] - 1
    while i >= 0 and st[i]["end"] >= start - QUIET_S:
        near.append(st[i])
        i -= 1
    i = stretch["last"] + 1
    while i < len(st) and st[i]["start"] <= end + QUIET_S:
        near.append(st[i])
        i += 1
    idle = [s for s in near if s["kind"] == "idle"]
    return bool(idle) and all(s["rise"] == 0 for s in idle)


def _relative(a: dict, ref: dict) -> dict:
    """a's weight over ref's, with a 95% interval from the two standard errors on the log scale."""
    value = a["value"] / ref["value"]
    out = {"value": value, "lo": None, "hi": None}
    if a["se"] is not None and ref["se"] is not None and a["value"] > 0:
        se_log = math.sqrt((a["se"] / a["value"]) ** 2 + (ref["se"] / ref["value"]) ** 2)
        half = t95(min(a["n"], ref["n"]) - 1) * se_log
        out.update(lo=value * math.exp(-half), hi=value * math.exp(half))
    return out


def fit_meter(readings: list[tuple], messages: list[dict], today: str) -> dict:
    st = steps(readings, messages)
    found = stretches(st)
    tails = {s["last"] for s in found if s["tail"]}
    kept = [s for s in found if quiet(st, s)]
    kinds = defaultdict(int)
    for s in st:
        kinds[s["kind"]] += 1
    untracked = [s for i, s in enumerate(st) if s["kind"] == "idle" and s["rise"] > 0 and i not in tails]
    out = {
        "readings": len(readings), "steps": dict(kinds),
        "idle_hours": sum(s["end"] - s["start"] for s in st if s["kind"] == "idle") / 3600,
        "untracked": {"steps": len(untracked), "points": sum(s["rise"] for s in untracked),
                      "hours": sum(s["end"] - s["start"] for s in untracked) / 3600},
        "stretches": {"found": len(found), "quiet": len(kept)},
        "basis": None, "basis_fit": {}, "reference": None, "groups": {}, "unmeasured": {}}
    by_group: dict[str, list[dict]] = defaultdict(list)
    for s in kept:
        by_group[s["group"]].append(s)
    total = sum(s["rise"] ** 2 for s in kept)
    for b in BASES:
        ests = {g: ratio_estimate([(s["rise"], s[b]) for s in ss]) for g, ss in by_group.items()}
        if total and all(ests.values()):
            res = sum((s["rise"] - ests[g]["value"] * s[b]) ** 2 for g, ss in by_group.items() for s in ss)
            out["basis_fit"][b] = res / total
    if out["basis_fit"]:
        out["basis"] = min(BASES, key=lambda b: out["basis_fit"].get(b, math.inf))
    basis = out["basis"]
    for g, ss in sorted(by_group.items()):
        rise = sum(s["rise"] for s in ss)
        entry = {"stretches": len(ss), "messages": sum(s["messages"] for s in ss), "rise": rise,
                 "hours": sum(st[s["last"]]["end"] - st[s["first"]]["start"] for s in ss) / 3600,
                 "tokens": {b: sum(s[b] for s in ss) for b in BASES}}
        if basis is None or len(ss) < MIN_STRETCHES or rise < MIN_POINTS:
            entry.update(status="not enough data",
                         needs=f"at least {MIN_STRETCHES} quiet stretches and {MIN_POINTS} points")
        else:
            entry.update(status="measured", basis=basis,
                         per_mtok=ratio_estimate([(s["rise"], s[basis] / 1e6) for s in ss], ROUNDING_VAR),
                         per_message=ratio_estimate([(s["rise"], s["messages"]) for s in ss], ROUNDING_VAR),
                         evidence={"kind": "measured", "status": "proposed", "date": today,
                                   "method": "trioditis refit: plan weights"})
        out["groups"][g] = entry
    measured = {g: e for g, e in out["groups"].items() if e["status"] == "measured"}
    if measured:
        ref = max(measured, key=lambda g: (measured[g]["stretches"], measured[g]["messages"]))
        out["reference"] = ref
        for g, e in measured.items():
            if g != ref:
                e["relative"] = _relative(e["per_mtok"], measured[ref]["per_mtok"])
    seen: dict[str, int] = defaultdict(int)
    for s in st:
        for g, t in s["groups"].items():
            if s["kind"] == "active":
                seen[g] += t["messages"]
    out["unmeasured"] = {g: n for g, n in sorted(seen.items(), key=lambda kv: -kv[1]) if g not in by_group}
    return out


def _rate(k: int | None, n: int | None) -> dict | None:
    if not n:
        return None
    lo, hi = wilson(k, n)
    return {"value": k / n, "lo": lo, "hi": hi, "n": n}


def signal_rates(conn, since: float, until: float) -> dict:
    out = {}
    for r in signal_counts(conn, since, until):
        rates = {"followed": _rate(r["followed"], r["followed_n"]),
                 "regenerated": _rate(r["regenerated"], r["regenerated_n"]),
                 "asked_again": _rate(r["asked_again"], r["asked_again_n"]),
                 "vote_up": _rate(r["vote_up"], r["vote_n"])}
        if any(rates.values()):
            out[r["key"]] = {"messages": r["messages"], **rates}
    return out


def fit_plan(conn, since: float, until: float) -> dict | None:
    """Per plan meter, the weights of each model and effort; plus the result-signal rates. None when the
    database has no plan tables (no plan import has run)."""
    if not has_tables(conn):
        return None
    today = time.strftime("%Y-%m-%d", time.localtime(until))
    messages = plan_messages(conn, since, until)
    meters = {}
    for (meter, scope), readings in sorted(meter_readings(conn, since, until).items()):
        mine = [m for m in messages if scope.lower() in m["model"].lower()] if scope else messages
        meters[f"{meter}:{scope}" if scope else meter] = {"scope": scope or None, **fit_meter(readings, mine, today)}
    return {"messages": len(messages), "meters": meters, "signals": signal_rates(conn, since, until),
            "caveats": list(CAVEATS), "review": "proposed: copy a weight into the team rules file only after review"}


def summary_lines(plan: dict | None) -> list[str]:
    if plan is None:
        return ["plan weights: no plan ledger yet (run `trioditis plan import`)"]
    lines = [f"plan weights from {plan['messages']:,} tracked messages (whole-percent, shared meters: only quiet "
             f"stretches with tracked use count; proposals, nothing applied):"]
    for name, m in plan["meters"].items():
        u = m["untracked"]
        if not m["stretches"]["found"]:
            lines.append(f"  {name}: {m['readings']} readings, no tracked use in them yet")
            continue
        head = (f"  {name}: {m['readings']} readings; {m['stretches']['quiet']} of {m['stretches']['found']} "
                f"single-model stretches quiet; it rose {_n(u['points'], 'point')} in readings with nothing tracked")
        if m["basis"]:
            other = ", ".join(f"{b} {v:.2f}" for b, v in m["basis_fit"].items() if b != m["basis"])
            head += (f"; {m['basis']} tokens fit best (residual share {m['basis_fit'][m['basis']]:.2f}"
                     + (f"; {other})" if other else ")"))
        lines.append(head)
        for g, e in m["groups"].items():
            if e["status"] != "measured":
                lines.append(f"    {g}: not enough data ({_n(e['stretches'], 'stretch')}, {_n(e['rise'], 'point')}; "
                             f"needs {e['needs']})")
                continue
            w, pm = e["per_mtok"], e["per_message"]
            line = (f"    {g}: {w['value']:.3g} points per 1M {e['basis']} tokens ({_ci(w)}), "
                    f"{pm['value']:.3g} per message; {_n(e['stretches'], 'stretch')}, {e['messages']:,} messages")
            if g == m["reference"]:
                line += " [reference]"
            elif e.get("relative"):
                r = e["relative"]
                line += f"; {r['value']:.2g}x {m['reference']} ({_ci(r)})"
            lines.append(line)
        if m["unmeasured"]:
            top = ", ".join(f"{g} ({n:,})" for g, n in list(m["unmeasured"].items())[:4])
            lines.append(f"    never alone in a quiet stretch: {top}")
    if not plan["signals"]:
        lines.append("  result signals: none recorded yet (Claude Code's transcripts carry none; Euboulia's events will)")
    return lines


def _ci(x: dict) -> str:
    return "no interval" if x["lo"] is None else f"95% {x['lo']:.3g}-{x['hi']:.3g}"


def _n(n: float, word: str) -> str:
    return f"{n:g} {word}" if n == 1 else f"{n:g} {word}{'es' if word.endswith('ch') else 's'}"
