"""Helpers for the route-accuracy study (docs/route-accuracy.md). Not part of the installed package.

    python scripts/route_study.py agreement docs/route-accuracy/labels.csv
    python scripts/route_study.py model-baseline docs/route-accuracy/labels.csv --model llama3.2:3b
    python scripts/route_study.py heldout docs/route-accuracy/labels.csv --settled docs/route-accuracy/settled.csv \
        --out trioditis/data/routes-heldout.csv

`agreement` reads a CSV with two label columns and prints how often the two labellers agree, Cohen's
kappa with a 95% interval, and a table of who said what. It makes no network call.

`model-baseline` asks a LOCAL model server to pick a route for every prompt from the route descriptions
in the config, with the route picker's own request (trioditis/router.py `RoutePickerClient`), and prints
how often it matches a label column. The one network call is to --base-url,
which must be on this machine: every prompt in the file is sent there and nowhere else.

`heldout` writes the label file for `trioditis eval-routes` from one set of the study: the route both
labellers gave where they agree, and the maintainer's decision (the `decision` column of --settled: a
route, or `drop`) where they don't. It makes no network call and never runs the router.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dataclasses  # noqa: E402

from trioditis.config import RoutePicker, load_config  # noqa: E402
from trioditis.measure import MeasureError, require_local  # noqa: E402
from trioditis.router import RoutePickerClient  # noqa: E402
from trioditis_metrics.stats import cohen_kappa, wilson  # noqa: E402


def read(path: str, columns: list[str]) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        sys.exit(f"route_study: {path} has no rows")
    missing = [c for c in columns if c not in rows[0]]
    if missing:
        sys.exit(f"route_study: {path} has no column named {missing[0]!r} (found: {', '.join(rows[0])})")
    return rows


def pct(k: int, n: int) -> str:
    lo, hi = wilson(k, n)
    return f"{k}/{n} = {100 * k / n:.1f}%  (95% interval {100 * lo:.1f}-{100 * hi:.1f}%)"


def groups(rows: list[dict], by: str | None) -> list[tuple[str, list[dict]]]:
    out = [("all rows", rows)]
    if by:
        for name in sorted({r[by] for r in rows}):
            out.append((f"{by} = {name or '(empty)'}", [r for r in rows if r[by] == name]))
    return out


def agreement(args) -> int:
    rows = [r for r in read(args.file, [args.a, args.b] + ([args.by] if args.by else []))
            if r[args.a].strip() and r[args.b].strip()]
    print(f"Agreement between {args.a} and {args.b}, {args.file}")
    for name, part in groups(rows, args.by):
        pairs = [(r[args.a].strip(), r[args.b].strip()) for r in part]
        k = cohen_kappa(pairs)
        print(f"\n{name}: n = {len(pairs)}")
        print(f"  same label {pct(sum(a == b for a, b in pairs), len(pairs))}")
        if k is None:
            print("  kappa: no value (both columns hold one and the same label)")
        else:
            print(f"  Cohen's kappa {k['kappa']:.2f}  (95% interval {k['lo']:.2f}-{k['hi']:.2f}, 2,000 bootstrap resamples)")
    labels = sorted({x for r in rows for x in (r[args.a].strip(), r[args.b].strip())})
    table = Counter((r[args.a].strip(), r[args.b].strip()) for r in rows)
    left = max(len(x) for x in labels + [args.a])
    print(f"\nWho said what, all rows (rows: {args.a}; columns: {args.b})")
    print("  " + " " * left + "".join(f"  {x:>9}" for x in labels))
    for a in labels:
        print(f"  {a:<{left}}" + "".join(f"  {table[(a, b)]:>9}" for b in labels))
    return 0


def ask(client: httpx.Client, url: str, picker: RoutePickerClient, prompt: str) -> str:
    """The route picker's own request (`RoutePickerClient.body`), so this baseline grades what the router sends."""
    reply = client.post(url, json=picker.body(prompt), timeout=300)
    reply.raise_for_status()
    return reply.json()["choices"][0]["message"]["content"] or ""


def model_baseline(args) -> int:
    try:
        require_local(args.base_url)
    except MeasureError as err:
        sys.exit(f"route_study: {err}")
    cfg = load_config(args.config)
    routes = list(cfg.routes)
    without = [r for r in routes if not cfg.routes[r].description]
    if without:
        sys.exit(f"route_study: route {without[0]!r} has no description in {args.config}; the model chooses from "
                 f"the descriptions, so add one")
    picker = RoutePickerClient(dataclasses.replace(cfg, route_picker=RoutePicker("local", args.model)), None)
    rows = read(args.file, ["prompt", args.label])
    url = args.base_url.rstrip("/") + "/chat/completions"
    answers: list[str] = []
    with httpx.Client(follow_redirects=False, trust_env=False) as client:
        for i, r in enumerate(rows, 1):
            answers.append(picker.parse(ask(client, url, picker, r["prompt"])) or "")  # "" = no route name in the reply
            if i % 50 == 0:
                print(f"  {i}/{len(rows)} asked", file=sys.stderr)
    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["prompt", "model_route"])
            w.writerows((r["prompt"], a) for r, a in zip(rows, answers, strict=True))
    for r, a in zip(rows, answers, strict=True):
        r["_answer"] = a
    print(f"Model baseline: {args.model} picks a route from the descriptions in {args.config}")
    print(f"labels: {args.file}, column {args.label}; rows with an empty label are left out")
    print(f"replies with no route name in them (counted as wrong): {sum(a == '' for a in answers)}")
    labelled = [r for r in rows if r[args.label].strip()]
    for name, part in groups(labelled, args.by):
        print(f"\n{name}\n  accuracy {pct(sum(r['_answer'] == r[args.label].strip() for r in part), len(part))}")
    return 0


def heldout(args) -> int:
    rows = [r for r in read(args.file, ["id", "set", "prompt", "stratum", "borderline", args.a, args.b])
            if r["set"] == args.set]
    if not rows:
        sys.exit(f"route_study: {args.file} has no rows with set = {args.set!r}")
    routes = sorted({r[args.a] for r in rows} | {r[args.b] for r in rows})
    decisions = {r["id"]: r["decision"].strip().lower() for r in read(args.settled, ["id", "decision"])}
    open_ids = [r["id"] for r in rows if r[args.a] != r[args.b]]
    extra = sorted(set(decisions) - set(open_ids), key=int)
    if extra:
        sys.exit(f"route_study: {args.settled} decides row {extra[0]}, but in {args.file} that row is not a "
                 f"disagreement in set {args.set!r}; remove it from {args.settled}")
    for i in open_ids:
        if not decisions.get(i):
            sys.exit(f"route_study: row {i} has no decision yet; in {args.settled} fill its `decision` cell "
                     f"with one of {', '.join(routes)}, or drop")
        if decisions[i] not in (*routes, "drop"):
            sys.exit(f"route_study: row {i} in {args.settled} has decision {decisions[i]!r}; "
                     f"use one of {', '.join(routes)}, or drop")
    out, said = [], Counter()
    for r in rows:
        if r[args.a] == r[args.b]:
            route = r[args.a]
        else:
            route = decisions[r["id"]]
            said["dropped" if route == "drop" else f"with {args.a}" if route == r[args.a]
                 else f"with {args.b}" if route == r[args.b] else "a third route"] += 1
            if route == "drop":
                continue
        out.append([r["prompt"], route, r["stratum"], r["borderline"]])
    with open(args.out, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["prompt", "expected_route", "stratum", "borderline"])
        w.writerows(out)
    print(f"set {args.set}: {len(rows)} prompts, both labels the same on {len(rows) - len(open_ids)}, "
          f"{len(open_ids)} decided by the maintainer")
    for name in sorted(said):
        print(f"  {name}: {said[name]}")
    per_route = Counter(row[1] for row in out)
    print(f"wrote {len(out)} rows to {args.out} ({', '.join(f'{n} {per_route[n]}' for n in sorted(per_route))})")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("agreement", help="how often two label columns agree, with Cohen's kappa (no network call)")
    a.add_argument("file")
    a.add_argument("--a", default="writer_route", help="first label column (default writer_route)")
    a.add_argument("--b", default="second_route", help="second label column (default second_route)")
    a.add_argument("--by", default="set", help="also report per value of this column (default set; '' for none)")
    a.set_defaults(run=agreement)
    m = sub.add_parser("model-baseline", help="a local model picks routes from their descriptions; sends the prompts to --base-url")
    m.add_argument("file")
    m.add_argument("--model", required=True, help="the model's name on the local server, for example llama3.2:3b")
    m.add_argument("--base-url", default="http://localhost:11434/v1", help="OpenAI-compatible local server (default Ollama's)")
    m.add_argument("--config", default="config.example.yaml", help="where the route descriptions come from")
    m.add_argument("--label", default="expected_route", help="the label column to score against (default expected_route)")
    m.add_argument("--by", default="set", help="also report per value of this column (default set; '' for none)")
    m.add_argument("--out", default=None, metavar="FILE", help="also save each prompt and the model's answer as CSV")
    m.set_defaults(run=model_baseline)
    h = sub.add_parser("heldout", help="write the eval-routes label file from agreed and settled labels (no network call)")
    h.add_argument("file")
    h.add_argument("--settled", required=True, metavar="FILE", help="CSV with id and decision (a route, or drop) for each disagreement")
    h.add_argument("--out", required=True, metavar="FILE", help="where the label file goes (prompt, expected_route, stratum, borderline)")
    h.add_argument("--set", default="second", help="which set to take (default second)")
    h.add_argument("--a", default="writer_route", help="first label column (default writer_route)")
    h.add_argument("--b", default="second_route", help="second label column (default second_route)")
    h.set_defaults(run=heldout)
    args = p.parse_args()
    return args.run(args)


if __name__ == "__main__":
    raise SystemExit(main())
