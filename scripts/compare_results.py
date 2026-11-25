"""Compare route pickers prompt by prompt, from the `--answers` files of `trioditis results`.

    python scripts/compare_results.py compare --baseline A.answers.json B.answers.json [C.answers.json ...]
    python scripts/compare_results.py changed X.answers.json Y.answers.json

`compare` scores each file against the shipped labels (trioditis/data/routes-heldout.csv) and sets every
candidate against the one baseline: right of n with a Wilson 95% interval; the prompts only the baseline got
right and only the candidate; the exact two-sided sign test on those; and the Holm-adjusted p over the k
candidates (Holm 1979: the smallest p times k, the next times k-1, and so on, each at least as large as the one
before, none above 1). A candidate is called "ahead and significant" only when it got more prompts right than
the baseline and its adjusted p is below 0.05. The sign test ignores the prompts both got right or both got
wrong: it asks whether, where the two disagree, one wins more often than a fair coin would give.

`changed` counts how many prompts got a different route between two runs, for the same model run twice or for
two models.

Only files written for the same held-out set as the one shipped are accepted: schema, keys, fingerprint, one
entry for each row and only the shipped routes (or "-" for no route) are checked, and anything else is refused
with the reason. It reads files only and sends nothing anywhere. The model names come from the files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from trioditis.evalroutes import read_labels  # noqa: E402
from trioditis.measure import ANSWERS_SCHEMA, NO_ROUTE, heldout_path, measured_config  # noqa: E402
from trioditis_metrics.stats import sign_test, wilson  # noqa: E402

ALPHA = 0.05
MAX_BYTES = 2_000_000
KEYS = {"schema", "trioditis", "heldout_sha256_16", "picker", "answers"}


class Refused(Exception):
    """The file is not used; the message says why."""


class Answers:
    def __init__(self, path: Path, picker: str, version: str, routes: dict[int, str]):
        self.path, self.picker, self.version, self.routes = path, picker, version, routes

    @property
    def name(self) -> str:
        return f"{self.picker} ({self.path.name})"


def shipped() -> tuple[list, str, list[str]]:
    """The shipped labels, their fingerprint (the file's sha256, 16 hex characters) and the shipped routes."""
    routes = list(measured_config("x", "http://127.0.0.1:1/v1").routes)
    return read_labels(heldout_path(), routes), hashlib.sha256(heldout_path().read_bytes()).hexdigest()[:16], routes


def load(path: Path, rows: list, fingerprint: str, routes: list[str]) -> Answers:
    """An answers file, checked completely; Refused otherwise."""
    try:
        if path.stat().st_size > MAX_BYTES:
            raise Refused(f"{path}: larger than {MAX_BYTES:,} bytes; this is not an answers file")
        doc = json.loads(path.read_text(encoding="utf-8"))
    except OSError as err:
        raise Refused(f"{path}: cannot read it ({err.strerror or err})") from None
    except ValueError:
        raise Refused(f"{path}: not JSON") from None
    if not isinstance(doc, dict) or set(doc) != KEYS:
        raise Refused(f"{path}: not an answers file (expected exactly the keys {sorted(KEYS)})")
    if doc["schema"] != ANSWERS_SCHEMA:
        raise Refused(f"{path}: schema is {doc['schema']!r}, this script reads {ANSWERS_SCHEMA!r}")
    if doc["heldout_sha256_16"] != fingerprint:
        raise Refused(f"{path}: written for a different held-out set (fingerprint {doc['heldout_sha256_16']!r}, "
                      f"the shipped set is {fingerprint!r}); the answers cannot be scored against these labels")
    if not isinstance(doc["picker"], str) or not doc["picker"] or not isinstance(doc["trioditis"], str):
        raise Refused(f"{path}: picker and trioditis must be text")
    answers = doc["answers"]
    if not isinstance(answers, list) or len(answers) != len(rows):
        raise Refused(f"{path}: expected {len(rows)} answers, one per held-out prompt")
    by_row: dict[int, str] = {}
    for item in answers:
        if not isinstance(item, dict) or set(item) != {"row", "route"} or type(item["row"]) is not int:
            raise Refused(f"{path}: every answer must be exactly {{row, route}} with a whole-number row")
        if item["route"] not in (*routes, NO_ROUTE):
            raise Refused(f"{path}: row {item['row']} names {item['route']!r}, which is not a shipped route or {NO_ROUTE!r}")
        if item["row"] in by_row:
            raise Refused(f"{path}: row {item['row']} appears twice")
        by_row[item["row"]] = item["route"]
    if set(by_row) != {r.number for r in rows}:
        raise Refused(f"{path}: its rows are not the shipped set's rows ({rows[0].number} to {rows[-1].number})")
    return Answers(path, doc["picker"], doc["trioditis"], by_row)


def right(rows: list, a: Answers) -> list[bool]:
    return [a.routes[r.number] == r.expected for r in rows]


def holm(ps: list[float]) -> list[float]:
    """Holm's step-down adjustment, in the order given: sorted ascending, the i-th smallest p times (k - i),
    then the running maximum so no adjusted p is smaller than an earlier one, capped at 1."""
    k = len(ps)
    adjusted = [0.0] * k
    running = 0.0
    for rank, i in enumerate(sorted(range(k), key=lambda j: ps[j])):
        running = max(running, min(1.0, (k - rank) * ps[i]))
        adjusted[i] = running
    return adjusted


def compare(baseline: Answers, candidates: list[Answers], rows: list) -> list[dict]:
    n = len(rows)
    base_right = right(rows, baseline)
    out = []
    for cand in candidates:
        cand_right = right(rows, cand)
        b = sum(x and not y for x, y in zip(base_right, cand_right, strict=True))
        c = sum(y and not x for x, y in zip(base_right, cand_right, strict=True))
        k = sum(cand_right)
        lo, hi = wilson(k, n)
        out.append({"name": cand.name, "right": k, "n": n, "low95": lo, "high95": hi, "only_baseline": b,
                    "only_candidate": c, "p": sign_test(b, c), "ahead": k > sum(base_right)})
    for row, p_holm in zip(out, holm([r["p"] for r in out]), strict=True):
        row["p_holm"] = p_holm
        row["verdict"] = ("ahead and significant" if row["ahead"] and p_holm < ALPHA
                          else "behind and significant" if row["right"] < sum(base_right) and p_holm < ALPHA
                          else "not shown to differ")
    return out


def changed(x: Answers, y: Answers, rows: list) -> dict:
    xr, yr = right(rows, x), right(rows, y)
    return {"n": len(rows), "changed": sum(x.routes[r.number] != y.routes[r.number] for r in rows),
            "right_only_first": sum(a and not b for a, b in zip(xr, yr, strict=True)),
            "right_only_second": sum(b and not a for a, b in zip(xr, yr, strict=True))}


def pct(x: float) -> str:
    return f"{100 * x:.1f}%"


def report_compare(baseline: Answers, results: list[dict], rows: list) -> list[str]:
    n = len(rows)
    base_k = sum(right(rows, baseline))
    lo, hi = wilson(base_k, n)
    lines = [f"Baseline {baseline.name}: {base_k}/{n} right = {pct(base_k / n)} (Wilson 95% interval {pct(lo)} to {pct(hi)})",
             f"{len(results)} candidate(s) against it, same {n} prompts; Holm over {len(results)} comparison(s), alpha {ALPHA}"]
    for r in results:
        lines += [f"{r['name']}: {r['right']}/{r['n']} right = {pct(r['right'] / r['n'])} "
                  f"(95% interval {pct(r['low95'])} to {pct(r['high95'])})",
                  f"  only the baseline right {r['only_baseline']}, only this one right {r['only_candidate']}; exact sign test "
                  f"p = {r['p']:.3g}, Holm-adjusted p = {r['p_holm']:.3g}: {r['verdict']}"]
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("compare", help="set each candidate against one baseline: right of n, sign test, Holm")
    c.add_argument("--baseline", required=True, type=Path, metavar="FILE", help="the answers file every candidate is set against")
    c.add_argument("candidates", nargs="+", type=Path, metavar="FILE", help="answers files of the models to compare")
    ch = sub.add_parser("changed", help="how many prompts got a different route between two runs")
    ch.add_argument("first", type=Path)
    ch.add_argument("second", type=Path)
    args = ap.parse_args(argv)
    try:
        rows, fingerprint, routes = shipped()
        if args.cmd == "compare":
            baseline = load(args.baseline, rows, fingerprint, routes)
            results = compare(baseline, [load(p, rows, fingerprint, routes) for p in args.candidates], rows)
            print("\n".join(report_compare(baseline, results, rows)))
        else:
            x, y = load(args.first, rows, fingerprint, routes), load(args.second, rows, fingerprint, routes)
            r = changed(x, y, rows)
            print(f"{x.name} and {y.name}: {r['changed']} of {r['n']} prompts got a different route ({pct(r['changed'] / r['n'])}); "
                  f"of those, right only in the first {r['right_only_first']}, right only in the second {r['right_only_second']}")
            if x.picker != y.picker:
                print(f"note: these are two different models ({x.picker}, {y.picker}), not two runs of one")
    except Refused as err:
        print(f"compare_results: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
