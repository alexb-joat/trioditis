"""Turn shared results (GitHub issues from the "Share results" form) into rows of docs/community-results.csv.

    gh issue list -R alexb-joat/trioditis --label results --state all --limit 500 --json number,body > issues.json
    python scripts/collect_results.py collect issues.json            # shows what it would add, writes nothing
    python scripts/collect_results.py collect issues.json --write    # adds the new rows
    python scripts/collect_results.py check trioditis-results.json   # checks one file the same way

Every submission is untrusted data: anyone can open an issue and type anything into the form. A submission
becomes a row only when the consent box is ticked, the JSON is a `trioditis-results.v1` file with exactly
the expected keys and types, its counts add up, and it was measured on the shipped held-out set. Anything
else is listed with the reason and left out. This script makes no network call: `gh` fetches the issues,
and you run it.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import io
import json
import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from trioditis.measure import SCHEMA, heldout_path  # noqa: E402

CSV_PATH = ROOT / "docs" / "community-results.csv"
MAX_BODY = 30_000              # the form's JSON is about 5,500 characters; far past that is not a results file
MAX_DEPTH = 6
MAX_STR = 120
ROUTES = ["code", "reasoning", "writing", "quick"]
NO_ROUTE = "-"
UNSAFE = re.compile(r"[\x00-\x1f\x7f<>`|\\]")

COLUMNS = ["issue", "date", "trioditis", "picker", "picker_digest_12", "server", "server_version", "embeddings",
           "os", "arch", "cpu", "cores", "ram_gb", "gpus", "n", "picker_right", "picker_low95", "picker_high95",
           "router", "router_right", "keywords_right", "only_picker", "only_router", "sign_test_p",
           "picker_no_route", "picker_failed", "picker_median_s", "timing_median_s_500", "timing_median_s_2000",
           "own_labels_n"]


class Rejected(Exception):
    """The submission is left out; the message says why."""


# ---------------------------------------------------------------- reading the issue

def _section(body: str, heading: str) -> str | None:
    m = re.search(rf"^###\s+{re.escape(heading)}\s*$(.*?)(?=^###\s|\Z)", body, re.M | re.S)
    return m.group(1).strip() if m else None


def from_issue(body: str) -> dict:
    """The results JSON from an issue body written by the form, once the consent box is ticked."""
    if not isinstance(body, str) or len(body) > MAX_BODY:
        raise Rejected(f"the issue body is missing or longer than {MAX_BODY:,} characters")
    sharing = _section(body, "Sharing")
    if not sharing or not re.search(r"^\s*[-*]\s+\[[xX]\]\s", sharing, re.M):
        raise Rejected("the consent box (public, CC0) is not ticked")
    text = _section(body, "Results file")
    if text is None:
        raise Rejected("no 'Results file' section: the issue was not written by the results form")
    while text.startswith("```"):                      # the form's own fence, and any the person added inside it
        rest = text.partition("\n")[2].rstrip()
        text = (rest[:-3] if rest.endswith("```") else rest).strip()
    return parse(text)


def parse(text: str) -> dict:
    if len(text) > MAX_BODY:
        raise Rejected(f"the results text is longer than {MAX_BODY:,} characters")
    try:
        doc = json.loads(text, parse_constant=lambda c: (_ for _ in ()).throw(Rejected(f"{c} is not a number")))
    except json.JSONDecodeError as e:
        raise Rejected(f"the results text is not JSON ({e.msg}, line {e.lineno})") from None
    _walk(doc, "file", 0)
    return doc


def _walk(x, where: str, depth: int) -> None:
    """Bounds every value: nesting depth, string length and characters, finite numbers."""
    if depth > MAX_DEPTH:
        raise Rejected(f"{where}: nested deeper than {MAX_DEPTH}")
    if isinstance(x, dict):
        for k, v in x.items():
            _walk(k, f"{where} key", depth + 1)
            _walk(v, f"{where}.{k}", depth + 1)
    elif isinstance(x, list):
        if len(x) > 16:
            raise Rejected(f"{where}: list longer than 16")
        for i, v in enumerate(x):
            _walk(v, f"{where}[{i}]", depth + 1)
    elif isinstance(x, str):
        if len(x) > MAX_STR or UNSAFE.search(x):
            raise Rejected(f"{where}: text longer than {MAX_STR} or with characters a results file never holds")
    elif isinstance(x, float) and not math.isfinite(x):
        raise Rejected(f"{where}: not a finite number")


# ---------------------------------------------------------------- checking the numbers

def _obj(x, where: str, keys: set[str], optional: set[str] = frozenset()) -> dict:
    if not isinstance(x, dict):
        raise Rejected(f"{where}: expected an object")
    missing, extra = keys - set(x), set(x) - keys - optional
    if missing or extra:
        raise Rejected(f"{where}: missing {sorted(missing)} / unexpected {sorted(extra)}")
    return x


def _int(x, where: str, lo: int = 0, hi: int = 10**6) -> int:
    if type(x) is not int or not lo <= x <= hi:
        raise Rejected(f"{where}: expected a whole number from {lo} to {hi}")
    return x


def _num(x, where: str, lo: float = 0.0, hi: float = 1e6, null: bool = False) -> float | None:
    if x is None and null:
        return None
    if type(x) not in (int, float) or not lo <= x <= hi:
        raise Rejected(f"{where}: expected a number from {lo} to {hi}")
    return x


def _str(x, where: str, pattern: str = r".{1,120}", null: bool = False) -> str | None:
    if x is None and null:
        return None
    if not isinstance(x, str) or not re.fullmatch(pattern, x):
        raise Rejected(f"{where}: unexpected text")
    return x


def _rate(x, where: str, n: int | None = None) -> dict:
    r = _obj(x, where, {"right", "n", "low95", "high95"}, {"per_route", "confusion", "no_route", "failed_after_retry",
                                                         "seconds_per_call"})
    _int(r["n"], f"{where}.n", 0, 10**5)
    if n is not None and r["n"] != n:
        raise Rejected(f"{where}.n is {r['n']}, expected {n}")
    _int(r["right"], f"{where}.right", 0, r["n"])
    for k in ("low95", "high95"):
        _num(r[k], f"{where}.{k}", 0, 1, null=True)
    return r


def _scored(x, where: str, n: int) -> dict:
    s = _rate(x, where, n)
    per = _obj(s.get("per_route"), f"{where}.per_route", set(ROUTES))
    for route in ROUTES:
        _rate(per[route], f"{where}.per_route.{route}")
    if sum(per[r]["right"] for r in ROUTES) != s["right"] or sum(per[r]["n"] for r in ROUTES) != n:
        raise Rejected(f"{where}: the routes don't add up to the total")
    conf = _obj(s.get("confusion"), f"{where}.confusion", set(), set(ROUTES))
    for route in ROUTES:
        row = _obj(conf.get(route, {}), f"{where}.confusion.{route}", set(), {*ROUTES, NO_ROUTE})
        counts = [_int(v, f"{where}.confusion.{route}.{k}") for k, v in row.items()]
        if sum(counts) != per[route]["n"] or row.get(route, 0) != per[route]["right"]:
            raise Rejected(f"{where}.confusion.{route} doesn't match its per-route count")
    return s


def heldout_fingerprint() -> str:
    return hashlib.sha256(heldout_path().read_bytes()).hexdigest()[:16]


def check(doc: dict) -> dict:
    """The CSV row for a valid results file; Rejected with the reason otherwise."""
    d = _obj(doc, "file", {"schema", "trioditis", "date", "settings", "models", "machine", "timing", "heldout",
                           "own_labels"})
    if d["schema"] != SCHEMA:
        raise Rejected(f"schema is {d['schema']!r}, this script reads {SCHEMA!r}")
    _str(d["trioditis"], "trioditis", r"\d{1,3}\.\d{1,3}\.\d{1,3}[a-z0-9.+-]{0,20}")
    _str(d["date"], "date", r"\d{4}-\d{2}-\d{2}")
    try:
        if dt.date.fromisoformat(d["date"]) > dt.date.today() + dt.timedelta(days=1):
            raise Rejected("date is in the future")
    except ValueError:
        raise Rejected("date is not a real date") from None

    st = _obj(d["settings"], "settings", {"config_sha256_16", "heldout_sha256_16", "instruction_sha256_16",
                                          "embeddings_threshold", "routes"})
    for k in ("config_sha256_16", "heldout_sha256_16", "instruction_sha256_16"):
        _str(st[k], f"settings.{k}", r"[0-9a-f]{16}")
    _num(st["embeddings_threshold"], "settings.embeddings_threshold", 0, 1)
    if st["routes"] != ROUTES:
        raise Rejected(f"settings.routes is {st['routes']}, the shipped routes are {ROUTES}")
    if st["heldout_sha256_16"] != heldout_fingerprint():
        raise Rejected("measured on a different held-out set than the one this repository ships")

    m = _obj(d["models"], "models", {"server", "server_version", "picker", "picker_digest", "embeddings",
                                     "embeddings_digest"})
    name = r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,99}"
    _str(m["server"], "models.server", name)
    _str(m["server_version"], "models.server_version", r"[0-9A-Za-z.+-]{1,40}", null=True)
    _str(m["picker"], "models.picker", name)
    _str(m["embeddings"], "models.embeddings", name)
    for k in ("picker_digest", "embeddings_digest"):
        _str(m[k], f"models.{k}", r"[0-9a-f]{64}", null=True)

    hw = _obj(d["machine"], "machine", {"os", "arch", "python", "cpu", "cores", "ram_gb", "unified_memory", "gpus"})
    _str(hw["os"], "machine.os", r"[A-Za-z]{1,20}")
    _str(hw["arch"], "machine.arch", r"[A-Za-z0-9_]{1,20}")
    _str(hw["python"], "machine.python", r"\d{1,2}\.\d{1,3}\.\d{1,3}[a-z0-9+]{0,10}")
    _str(hw["cpu"], "machine.cpu", null=True)
    cores = _int(hw["cores"], "machine.cores", 1, 4096) if hw["cores"] is not None else None
    _num(hw["ram_gb"], "machine.ram_gb", 0, 1e5, null=True)
    if hw["unified_memory"] not in (True, False, None):
        raise Rejected("machine.unified_memory: expected true, false or null")
    if not isinstance(hw["gpus"], list):
        raise Rejected("machine.gpus: expected a list")
    for i, g in enumerate(hw["gpus"]):
        g = _obj(g, f"machine.gpus[{i}]", {"name", "vendor", "shared", "vram_gb"})
        _str(g["name"], f"machine.gpus[{i}].name")
        _str(g["vendor"], f"machine.gpus[{i}].vendor", r"[a-z]{1,20}", null=True)
        _num(g["vram_gb"], f"machine.gpus[{i}].vram_gb", 0, 1e4, null=True)
        if g["shared"] not in (True, False, None):
            raise Rejected(f"machine.gpus[{i}].shared: expected true, false or null")

    t = _obj(d["timing"], "timing", {"repeats", "failed", "by_chars"})
    _int(t["repeats"], "timing.repeats", 0, 100)
    _int(t["failed"], "timing.failed", 0, 10**4)
    medians = {}
    for i, b in enumerate(t["by_chars"] if isinstance(t["by_chars"], list) else [None]):
        b = _obj(b, f"timing.by_chars[{i}]", {"chars", "n"}, {"median", "p90", "min", "max"})
        medians[_int(b["chars"], f"timing.by_chars[{i}].chars", 1, 10**5)] = (
            _num(b.get("median"), f"timing.by_chars[{i}].median", 0, 3600, null=True))

    h = _obj(d["heldout"], "heldout", {"n", "picker", "router_keywords", "router", "router_note", "picker_vs_router"})
    n = _int(h["n"], "heldout.n", 1, 10**5)
    picker = _scored(h["picker"], "heldout.picker", n)
    keywords = _scored(h["router_keywords"], "heldout.router_keywords", n)
    router = _scored(h["router"], "heldout.router", n) if h["router"] is not None else None
    _str(h["router_note"], "heldout.router_note", null=True)
    pr = _obj(h["picker_vs_router"], "heldout.picker_vs_router", {"only_first", "only_second", "sign_test_p", "router"})
    if pr["router"] != ("router" if router else "router_keywords"):
        raise Rejected("heldout.picker_vs_router.router doesn't name the router row that was measured")
    _int(pr["only_first"], "heldout.picker_vs_router.only_first", 0, n)
    _int(pr["only_second"], "heldout.picker_vs_router.only_second", 0, n)
    _num(pr["sign_test_p"], "heldout.picker_vs_router.sign_test_p", 0, 1)
    second = router or keywords
    if pr["only_first"] - pr["only_second"] != picker["right"] - second["right"]:
        raise Rejected("heldout.picker_vs_router doesn't match the two right counts")
    no_route = _int(picker.get("no_route"), "heldout.picker.no_route", 0, n)
    failed = _int(picker.get("failed_after_retry"), "heldout.picker.failed_after_retry", 0, n)
    spc = _obj(picker.get("seconds_per_call"), "heldout.picker.seconds_per_call", {"n"}, {"median", "p90", "min", "max"})
    per_call = _num(spc.get("median"), "heldout.picker.seconds_per_call.median", 0, 3600, null=True)

    own = d["own_labels"]
    own_n = None if own is None else _int(_obj(own, "own_labels", {"n"}, {"picker", "router", "router_uses_embeddings",
                                                                          "picker_vs_router"})["n"], "own_labels.n")
    gpus = "; ".join(g["name"] for g in hw["gpus"])
    return {"date": d["date"], "trioditis": d["trioditis"], "picker": m["picker"],
            "picker_digest_12": (m["picker_digest"] or "")[:12], "server": m["server"],
            "server_version": m["server_version"] or "", "embeddings": m["embeddings"], "os": hw["os"],
            "arch": hw["arch"], "cpu": hw["cpu"] or "", "cores": cores if cores is not None else "",
            "ram_gb": hw["ram_gb"] if hw["ram_gb"] is not None else "", "gpus": gpus, "n": n,
            "picker_right": picker["right"], "picker_low95": picker["low95"], "picker_high95": picker["high95"],
            "router": pr["router"], "router_right": second["right"], "keywords_right": keywords["right"],
            "only_picker": pr["only_first"], "only_router": pr["only_second"], "sign_test_p": pr["sign_test_p"],
            "picker_no_route": no_route, "picker_failed": failed, "picker_median_s": "" if per_call is None else per_call,
            "timing_median_s_500": medians.get(500) if medians.get(500) is not None else "",
            "timing_median_s_2000": medians.get(2000) if medians.get(2000) is not None else "",
            "own_labels_n": "" if own_n is None else own_n}


# ---------------------------------------------------------------- the CSV

def _cell(v) -> str:
    """Text a spreadsheet will never run as a formula."""
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@") else s


def read_rows(path: Path) -> dict[int, dict]:
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as f:
        return {int(r["issue"]): r for r in csv.DictReader(f)}


def render(rows: dict[int, dict]) -> str:
    out = io.StringIO()
    w = csv.DictWriter(out, COLUMNS, lineterminator="\n")
    w.writeheader()
    for issue in sorted(rows):
        w.writerow({k: _cell(rows[issue].get(k)) for k in COLUMNS})
    return out.getvalue()


def collect(issues: list, existing: dict[int, dict]) -> tuple[dict[int, dict], list[str]]:
    """New rows by issue number, and one line per issue left out. Rows already in the CSV are kept as they are."""
    new, notes = {}, []
    if not isinstance(issues, list):
        raise SystemExit("error: the issues file must be the JSON list `gh issue list ... --json number,body` prints")
    for item in issues:
        number = item.get("number") if isinstance(item, dict) else None
        if type(number) is not int or number < 1:
            notes.append("skipped an entry with no issue number")
            continue
        if number in existing:
            continue
        try:
            new[number] = {"issue": number, **check(from_issue(item.get("body")))}
        except Rejected as e:
            notes.append(f"#{number}: left out: {e}")
    return new, notes


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect", help="add shared results from a `gh issue list` JSON file to the CSV")
    c.add_argument("issues", type=Path)
    c.add_argument("--csv", type=Path, default=CSV_PATH)
    c.add_argument("--write", action="store_true", help="write the new rows (without it, only show them)")
    k = sub.add_parser("check", help="check results files the way a submission is checked")
    k.add_argument("files", type=Path, nargs="+")
    args = ap.parse_args(argv)

    if args.cmd == "check":
        bad = 0
        for p in args.files:
            try:
                row = check(parse(p.read_text(encoding="utf-8")))
                print(f"{p}: ok, picker {row['picker']} {row['picker_right']}/{row['n']}, "
                      f"{row['router']} {row['router_right']}/{row['n']}")
            except Rejected as e:
                bad += 1
                print(f"{p}: left out: {e}")
        return 1 if bad else 0

    existing = read_rows(args.csv)
    new, notes = collect(json.loads(args.issues.read_text(encoding="utf-8")), existing)
    for line in notes:
        print(line)
    for issue, row in sorted(new.items()):
        print(f"#{issue}: new row: {row['picker']} {row['picker_right']}/{row['n']} on {row['os']} {row['arch']}, "
              f"{row['cpu'] or 'CPU unknown'}")
    if not new:
        print(f"nothing new for {args.csv.name} ({len(existing)} rows already there)")
    elif args.write:
        args.csv.write_text(render({**existing, **new}), encoding="utf-8")
        print(f"wrote {len(new)} new rows to {args.csv} ({len(existing) + len(new)} in all)")
    else:
        print(f"{len(new)} new rows; nothing written. Add --write to write them to {args.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
