"""Helpers for the route-accuracy study (docs/route-accuracy.md). Not part of the installed package.

    python scripts/route_study.py agreement docs/route-accuracy/labels.csv
    python scripts/route_study.py model-baseline docs/route-accuracy/labels.csv --model llama3.2:3b

`agreement` reads a CSV with two label columns and prints how often the two labellers agree, Cohen's
kappa with a 95% interval, and a table of who said what. It makes no network call.

`model-baseline` asks a LOCAL model server to pick a route for every prompt from the route descriptions
in the config, and prints how often it matches a label column. The one network call is to --base-url,
which must be on this machine: every prompt in the file is sent there and nowhere else.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trioditis.config import load_config  # noqa: E402
from trioditis_metrics.stats import cohen_kappa, wilson  # noqa: E402

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


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


def ask(client: httpx.Client, url: str, model: str, system: str, prompt: str) -> str:
    body = {"model": model, "temperature": 0, "seed": 0, "max_tokens": 8,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": f"Message:\n<<<\n{prompt}\n>>>\nRoute:"}]}
    reply = client.post(url, json=body, timeout=300)
    reply.raise_for_status()
    return reply.json()["choices"][0]["message"]["content"] or ""


def model_baseline(args) -> int:
    host = urlsplit(args.base_url).hostname
    if host not in LOCAL_HOSTS:
        sys.exit(f"route_study: --base-url {args.base_url} is not on this machine; this command sends every prompt "
                 f"in the file to that address, so it only talks to localhost")
    cfg = load_config(args.config)
    routes = list(cfg.routes)
    without = [r for r in routes if not cfg.routes[r].description]
    if without:
        sys.exit(f"route_study: route {without[0]!r} has no description in {args.config}; the model chooses from "
                 f"the descriptions, so add one")
    system = ("You sort messages for a router. Routes:\n"
              + "".join(f"- {r}: {cfg.routes[r].description}\n" for r in routes)
              + "Reply with one word: the name of the route that fits the message best.")
    rows = read(args.file, ["prompt", args.label])
    url = args.base_url.rstrip("/") + "/chat/completions"
    answers: list[str] = []
    with httpx.Client(follow_redirects=False, trust_env=False) as client:
        for i, r in enumerate(rows, 1):
            text = ask(client, url, args.model, system, r["prompt"]).strip().lower()
            words = [w.strip(".,:;`'\"*") for w in text.split()]
            answers.append(next((w for w in words if w in routes), ""))  # "" = no route name in the reply
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
    args = p.parse_args()
    return args.run(args)


if __name__ == "__main__":
    raise SystemExit(main())
