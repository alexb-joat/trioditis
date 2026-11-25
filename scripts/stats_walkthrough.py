"""A worked example of the paired statistics in `trioditis_metrics.stats`, on the public development set.

    python scripts/stats_walkthrough.py

Two route pickers answer the same 100 prompts (`tests/fixtures/routes.csv`, 25 per route):

- `keywords`: the router's keyword rule alone, run here in process on `config.example.yaml` with no embedder.
  The script refuses a config that has a `route_picker`, so no model is ever asked.
- `llama3.2:3b`: the routes a small local model chose in one saved run,
  `docs/route-accuracy/baseline-llama3.2-3b.csv` (415 prompts), kept to the 100 development prompts and
  matched on the prompt text. The script refuses when one of the 100 is missing there.

It reads only committed files, makes no network call and prints no date, so `docs/eval-statistics.md` holds
its output word for word (a test checks that). The numbers teach the methods; they are not a finding about
either picker (the page says why).
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from trioditis.config import load_config  # noqa: E402
from trioditis.evalroutes import classify, read_labels  # noqa: E402
from trioditis_metrics.stats import (  # noqa: E402
    discordant_needed,
    mcnemar,
    paired_counts,
    paired_difference,
    sign_test_power,
    stratified_proportion,
    stratified_sample,
    wilson,
)

LABELS = ROOT / "tests" / "fixtures" / "routes.csv"
CONFIG = ROOT / "config.example.yaml"
BASELINE = ROOT / "docs" / "route-accuracy" / "baseline-llama3.2-3b.csv"
FIRST, SECOND = "keywords", "llama3.2:3b"
SAMPLE_SIZE, SAMPLE_SEED = 20, 0


class Refused(Exception):
    """The example is not run; the message says why."""


def pct(x: float) -> str:
    return f"{100 * x:.1f}%"


def rate(k: int, n: int) -> str:
    lo, hi = wilson(k, n)
    return f"{k}/{n} = {pct(k / n)} ({pct(lo)} to {pct(hi)})"


def keyword_picks(cfg, rows: list) -> list[str]:
    """The router's keyword-only pick for each prompt: `classify` with no embedder, so no call goes anywhere."""
    if cfg.route_picker is not None:
        raise Refused("the config has a route_picker, which would ask a model; this example uses the keyword rule only "
                      "(config.example.yaml has none)")

    async def all_picks() -> list[str]:
        return [await classify(cfg, r.prompt, None) for r in rows]

    return asyncio.run(all_picks())


def saved_picks(path: Path, rows: list, routes: list[str]) -> list[str]:
    """The saved model's route for each labelled prompt, matched on the stripped prompt text."""
    try:
        with path.open(encoding="utf-8", newline="") as fh:
            saved = {d["prompt"].strip(): d["model_route"] for d in csv.DictReader(fh)}
    except (OSError, KeyError) as err:
        raise Refused(f"{path}: cannot read it as a table with prompt and model_route columns ({err})") from None
    missing = [r.number for r in rows if r.prompt.strip() not in saved]
    if missing:
        raise Refused(f"{path}: no saved pick for {len(missing)} of the {len(rows)} prompts (label rows {missing[:5]}...)")
    picks = [saved[r.prompt.strip()] for r in rows]
    if bad := sorted({p for p in picks if p not in routes}):
        raise Refused(f"{path}: saved picks that are not routes of the config: {bad}")
    return picks


def build(labels: Path = LABELS, config: Path = CONFIG, baseline: Path = BASELINE) -> list[str]:
    """The whole example as printed lines."""
    cfg = load_config(config)
    routes = list(cfg.routes)
    rows = read_labels(labels, routes)
    n = len(rows)
    first, second = keyword_picks(cfg, rows), saved_picks(baseline, rows, routes)
    first_right = [p == r.expected for p, r in zip(first, rows, strict=True)]
    second_right = [p == r.expected for p, r in zip(second, rows, strict=True)]
    counts = paired_counts(first_right, second_right)
    b, c = counts["only_first"], counts["only_second"]
    m = mcnemar(b, c)
    diff, low, high = paired_difference(b, c, n)
    w = max(len(FIRST), len(SECOND))
    out = [f"Two route pickers on the same {n} public development prompts ({n // len(routes)} per route)",
           f"  {FIRST:<{w}}  the router's keyword rule alone (config.example.yaml); no model is asked",
           f"  {SECOND:<{w}}  a small local model choosing from the route descriptions; one saved run",
           "",
           f"1. Right of {n}, with the Wilson 95% interval",
           f"  {FIRST:<{w}}  {rate(sum(first_right), n)}",
           f"  {SECOND:<{w}}  {rate(sum(second_right), n)}",
           "",
           "2. The paired table: how the two pickers share the prompts",
           f"  {'':{w + 6}}{SECOND + ' right':>20}{SECOND + ' wrong':>20}",
           f"  {FIRST + ' right':<{w + 6}}{counts['both_right']:>20}{counts['only_first']:>20}",
           f"  {FIRST + ' wrong':<{w + 6}}{counts['only_second']:>20}{counts['both_wrong']:>20}",
           "",
           f"3. McNemar's test on the {m['discordant']} prompts where they disagree "
           f"(only {FIRST} right {b}, only {SECOND} right {c})",
           f"  exact two-sided p  {m['exact_p']:.3f}",
           f"  mid-p              {m['mid_p']:.3f}",
           f"  chi-square         {m['chi2']:.3f}, p {m['chi2_p']:.3f}",
           f"  with correction    {m['chi2_cc']:.3f}, p {m['chi2_cc_p']:.3f}",
           "",
           f"4. The difference in the share right, {FIRST} minus {SECOND} (Wald+2 interval)",
           f"  {100 * diff:+.1f} points ({100 * low:+.1f} to {100 * high:+.1f})",
           "",
           f"5. Power: the chance the exact test finds a real difference, given {m['discordant']} disagreements",
           f"  a 65:35 split  {sign_test_power(m['discordant'], 0.65):.3f}",
           f"  a 60:40 split  {sign_test_power(m['discordant'], 0.60):.3f}",
           "  disagreements needed for 80% power: the first count that reaches it; the count from which every count does"]
    for share, label in ((0.65, "a 65:35 split"), (0.60, "a 60:40 split")):
        needed_first, needed_stable = discordant_needed(share)
        out.append(f"  {label}  {needed_first}; {needed_stable}")
    out += ["", f"6. Right of {n // len(routes)} on each route, with the Wilson 95% interval (a description, not a test)",
            f"  {'route':<11}{FIRST:<40}{SECOND}"]
    for route in routes:
        of_route = [(a, b2) for a, b2, r in zip(first_right, second_right, rows, strict=True) if r.expected == route]
        out.append(f"  {route:<11}{rate(sum(a for a, _ in of_route), len(of_route)):<40}"
                   f"{rate(sum(b2 for _, b2 in of_route), len(of_route))}")
    sample = stratified_sample(rows, key=lambda r: r.expected, size=SAMPLE_SIZE, seed=SAMPLE_SEED)
    right_by_number = dict(zip((r.number for r in rows), first_right, strict=True))
    outcomes = {route: [int(right_by_number[r.number]) for r in sample if r.expected == route] for route in routes}
    est = stratified_proportion(outcomes, dict(Counter(r.expected for r in rows)))
    full = sum(first_right) / n
    out += ["", f"7. A stratified sample of {SAMPLE_SIZE} prompts ({SAMPLE_SIZE // len(routes)} per route, seed {SAMPLE_SEED}), "
                f"{FIRST} only",
            f"  right in the sample  {sum(map(sum, outcomes.values()))}/{len(sample)}",
            f"  estimate for the full set  {pct(est['estimate'])}, standard error {100 * est['se']:.1f} points, "
            f"95% interval {pct(est['low'])} to {pct(est['high'])}",
            f"  the full set, all {n} prompts  {rate(sum(first_right), n)}",
            f"  the interval {'holds' if est['low'] <= full <= est['high'] else 'misses'} the full-set value"]
    return out


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description=__doc__.split("\n\n")[0]).parse_args(argv)
    try:
        print("\n".join(build()))
    except Refused as err:
        print(f"stats_walkthrough: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
