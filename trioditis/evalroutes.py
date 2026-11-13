"""`trioditis eval-routes`: how often does the router pick the right route? Offline, against a labelled CSV.

It calls no chat model and sends no prompt anywhere except, when the config has an `embeddings:`
block, to that block's LOCAL provider (the one network call, the same one the router makes).

Which code is graded. A `model: auto` request with one user message reaches `router._classify` with
`last_user_text(messages)` and no session pin or tools, and the route it returns is the route
`decide()` reports. This module calls `_classify` directly, on text from `last_user_text`, instead of
`decide()`. Reason: after the route is chosen, `decide()` filters models by API key, context size and
privacy and raises when none can answer, which says nothing about the route and would make the grade
depend on which keys the machine running it happens to hold. No classification logic is copied here, so
the number grades the code the router runs, and a change to `_classify` changes the number.

Two rows are graded: keywords only (`embedder=None`) and keywords plus embeddings. Each prompt is
embedded once (`Memo`); the threshold sweep then re-classifies from memory. The sweep picks the
`embeddings.threshold` that scores best on one half of the labels and reports it on the other half,
so the reported number is not the one the choice was tuned on. A better value is only proposed in the
notebook, and only when the sign test says the gain is unlikely to be chance; nothing in the config is changed.
"""

from __future__ import annotations

import asyncio
import csv
import dataclasses
import datetime as dt
import difflib
import hashlib
import json
import math
import sys
import textwrap
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import httpx

from trioditis_metrics.stats import sign_test, wilson

from .config import Config
from .refit import _append_private, notebook_path
from .router import Embedder, _classify, last_user_text, tool_names

GRID = range(40, 81, 5)   # the thresholds tried, in hundredths: 0.40 ... 0.80 (integers, so ties never depend on float error)
MIN_SWEEP_ROWS = 20       # fewer labelled prompts than this cannot be split into halves worth comparing
ALPHA = 0.05              # a proposal needs a sign-test p-value below this
WIDTH = 100
REQUIRED = ("prompt", "expected_route")


class LabelsError(Exception):
    """A labels file that can't be graded; the message names the cause, the place and the fix."""


@dataclass(frozen=True)
class Row:
    number: int      # spreadsheet row: the header is row 1
    prompt: str
    expected: str


# ---------------------------------------------------------------- reading the labels

def _name(h: str) -> str:
    """A header cell compared loosely: 'Expected Route ' and 'expected-route' both mean expected_route."""
    return "_".join(h.replace("-", " ").lower().split())


def _read_records(p: Path, shown: str) -> list[list[str]]:
    records: list[list[str]] = []
    try:
        with open(p, encoding="utf-8-sig", newline="") as f:  # utf-8-sig: spreadsheets add a BOM
            reader = csv.reader(f, strict=True)
            while True:
                try:
                    rec = next(reader)
                except StopIteration:
                    break
                except csv.Error as err:  # the record being read is the one after the last complete one
                    at, msg = f"{shown} row {len(records) + 1}", str(err)
                    if "unexpected end of data" in msg:
                        raise LabelsError(f"{at}: a quote is opened and never closed; close it, or write each quote "
                                          f"inside a prompt twice (\"\")") from None
                    if "field larger than field limit" in msg:
                        raise LabelsError(f"{at}: a prompt is longer than {csv.field_size_limit()} characters, or a quote "
                                          f"is never closed; shorten the prompt or close the quote") from None
                    raise LabelsError(f"{at}: {msg}; wrap the whole prompt in quotes and write each quote inside it "
                                      f"twice (\"\")") from None
                records.append(rec)
    except FileNotFoundError:
        raise LabelsError(f"labels file not found: {shown}; pass a CSV with the columns "
                          f"{', '.join(REQUIRED)} as --labels FILE") from None
    except UnicodeDecodeError as err:
        raise LabelsError(f"{shown} is not UTF-8 text (byte {err.start}); save it as CSV UTF-8") from None
    except OSError as err:
        raise LabelsError(f"cannot read labels file {shown}: {err.strerror or err}") from None
    return records


def read_labels(path: str | Path, routes) -> list[Row]:
    shown = str(path)
    records = _read_records(Path(path), shown)
    if not records:
        raise LabelsError(f"{shown} is empty; it needs a header row ({', '.join(REQUIRED)}) and one row per prompt")
    found = ", ".join(h.strip() for h in records[0] if h.strip()) or "nothing"
    header = [_name(h) for h in records[0]]
    missing = [c for c in REQUIRED if c not in header]
    if len(missing) == len(REQUIRED):
        raise LabelsError(f"{shown} row 1: this does not look like a header: it has neither a 'prompt' nor an "
                          f"'expected_route' column (found: {found}); make row 1 the column names")
    if missing:
        raise LabelsError(f"{shown} row 1: no column named {missing[0]!r} (found: {found}); add it to the header row")
    ip, ie = header.index("prompt"), header.index("expected_route")
    rows: list[Row] = []
    first_seen: dict[str, int] = {}
    unknown: list[tuple[int, str]] = []
    for number, rec in enumerate(records[1:], start=2):
        if not any(c.strip() for c in rec):
            continue  # a blank line
        for col, name in ((ip, "prompt"), (ie, "expected_route")):
            if col >= len(rec):
                raise LabelsError(f"{shown} row {number} has no {name} (only {len(rec)} column(s)); "
                                  f"add the missing cell or delete the row")
        prompt, expected = rec[ip], rec[ie].strip()
        if not prompt.strip():
            raise LabelsError(f"{shown} row {number}: prompt is empty; fill it in or delete the row")
        if not expected:
            raise LabelsError(f"{shown} row {number}: expected_route is empty; use one of {', '.join(routes)}")
        key = prompt.strip()
        if key in first_seen:
            raise LabelsError(f"{shown} rows {first_seen[key]} and {number} have the same prompt; remove one "
                              f"(a prompt counted twice makes the result look more certain than it is)")
        first_seen[key] = number
        if expected not in routes:
            unknown.append((number, expected))
        rows.append(Row(number, prompt, expected))
    if unknown:
        number, bad = unknown[0]
        near = difflib.get_close_matches(bad, list(routes), n=1)
        more = f" (and {len(unknown) - 1} more row(s))" if len(unknown) > 1 else ""
        raise LabelsError(f"{shown} row {number}: expected_route {bad!r} is not a route in the config{more}; "
                          f"its routes are {', '.join(routes)}"
                          + (f" (did you mean {near[0]!r}?)" if near else "") + "; fix the label or add the route")
    if not rows:
        raise LabelsError(f"{shown} has a header but no rows; add one line per prompt with its expected route")
    return rows


# ---------------------------------------------------------------- the embedder, asked once per prompt

class Similarities(Protocol):
    async def similarities(self, text: str) -> dict[str, float]: ...


class Memo:
    """Wraps an embedder with the same `similarities` interface and remembers every answer, so the
    sweep re-classifies without embedding again. A failure is raised, never remembered."""

    def __init__(self, inner: Similarities):
        self.inner = inner
        self._seen: dict[str, dict[str, float]] = {}

    async def similarities(self, text: str) -> dict[str, float]:
        if text not in self._seen:
            self._seen[text] = await self.inner.similarities(text)
        return self._seen[text]

    def answered(self) -> bool:
        """True when at least one prompt got a similarity per route (the routes have `examples`)."""
        return any(self._seen.values())


def request_text(prompt: str) -> str:
    """The text the router classifies for a `model: auto` request holding this one user message."""
    return last_user_text([{"role": "user", "content": prompt}])


async def classify(cfg: Config, prompt: str, embedder: Similarities | None) -> str:
    body = {"model": "auto", "messages": [{"role": "user", "content": prompt}]}
    return await _classify(cfg, request_text(prompt), embedder, [], tool_names(body))


# ---------------------------------------------------------------- scoring

@dataclass
class Score:
    label: str
    expected: list[str]
    predicted: list[str]

    @property
    def n(self) -> int:
        return len(self.expected)

    @property
    def k(self) -> int:
        return sum(e == p for e, p in zip(self.expected, self.predicted, strict=True))

    def per_route(self, route: str) -> tuple[int, int]:
        pairs = [(e, p) for e, p in zip(self.expected, self.predicted, strict=True) if e == route]
        return sum(e == p for e, p in pairs), len(pairs)

    def confusion(self, expected: str, predicted: str) -> int:
        return sum(e == expected and p == predicted for e, p in zip(self.expected, self.predicted, strict=True))


@dataclass
class Candidate:
    hundredths: int          # the value on the integer grid (the current one, if off the grid, rounded)
    value: float
    current: bool
    right: int = 0           # correct on the pick half


@dataclass
class Sweep:
    current: float
    skipped: str | None = None                      # why there is no sweep result
    n_pick: int = 0
    n_report: int = 0
    candidates: list[Candidate] = field(default_factory=list)
    picked: Candidate | None = None
    current_ok: list[bool] = field(default_factory=list)   # per report-half row, right under the current threshold
    picked_ok: list[bool] = field(default_factory=list)    # ... under the picked one

    @property
    def same(self) -> bool:
        return self.picked is not None and self.picked.current

    @property
    def only_current(self) -> int:
        """Report-half rows the current threshold got right and the picked one wrong."""
        return sum(a and not b for a, b in zip(self.current_ok, self.picked_ok, strict=True))

    @property
    def only_picked(self) -> int:
        return sum(b and not a for a, b in zip(self.current_ok, self.picked_ok, strict=True))

    @property
    def p(self) -> float:
        return sign_test(self.only_current, self.only_picked)

    @property
    def better(self) -> bool:
        """The picked threshold scored higher on the report half (before asking whether that could be chance)."""
        return not self.same and sum(self.picked_ok) > sum(self.current_ok)

    @property
    def proposes(self) -> bool:
        return self.skipped is None and self.better and self.p < ALPHA


@dataclass
class Evaluation:
    keywords: Score
    embeddings: Score | None = None
    embeddings_note: str | None = None   # why there is no second row
    sweep: Sweep | None = None


def _order(prompt: str) -> bytes:
    return hashlib.sha256(prompt.encode("utf-8")).digest()


def split(rows: list[Row]) -> tuple[list[Row], list[Row]]:
    """Two exact halves, whatever the order of the file: rows sorted by SHA-256 of the prompt, the first
    ceil(n/2) pick the threshold, the rest report it. No random state; adding a row moves the cut."""
    ordered = sorted(rows, key=lambda r: _order(r.prompt))
    cut = math.ceil(len(ordered) / 2)
    return ordered[:cut], ordered[cut:]


async def _score(cfg: Config, rows: list[Row], embedder: Similarities | None, label: str) -> Score:
    predicted = [await classify(cfg, r.prompt, embedder) for r in rows]
    return Score(label, [r.expected for r in rows], predicted)


def _skip_reason(cfg: Config, n: int) -> str | None:
    if len(cfg.routes) < 2:
        return "the config has one route, so there is no choice for a threshold to change"
    if n < MIN_SWEEP_ROWS:
        return f"only {n} labelled prompts; the sweep needs at least {MIN_SWEEP_ROWS} to split into two halves worth comparing"
    return None


async def _sweep(cfg: Config, rows: list[Row], memo: Memo) -> Sweep:
    emb = cfg.embeddings
    if emb is None:
        raise ValueError("the sweep needs an embeddings: block")
    current = emb.threshold
    sw = Sweep(current)
    sw.skipped = _skip_reason(cfg, len(rows))
    if sw.skipped:
        return sw
    pick, report = split(rows)
    sw.n_pick, sw.n_report = len(pick), len(report)

    async def right(rs: list[Row], t: float) -> list[bool]:
        c = dataclasses.replace(cfg, embeddings=dataclasses.replace(emb, threshold=t))
        return [await classify(c, r.prompt, memo) == r.expected for r in rs]

    cur_h = round(current * 100)
    on_grid = cur_h in GRID and abs(cur_h / 100 - current) < 1e-9
    sw.candidates = [Candidate(h, current if (on_grid and h == cur_h) else h / 100, on_grid and h == cur_h) for h in GRID]
    if not on_grid:
        sw.candidates.append(Candidate(cur_h, current, True))
    sw.candidates.sort(key=lambda c: (c.hundredths, not c.current))
    for c in sw.candidates:
        c.right = sum(await right(pick, c.value))
    # Best on the pick half; ties go to the value closest to the current one (in hundredths), then the lower one,
    # then the current one itself.
    sw.picked = min(sw.candidates, key=lambda c: (-c.right, abs(c.hundredths - cur_h), c.hundredths, not c.current))
    sw.current_ok = await right(report, current)
    sw.picked_ok = sw.current_ok if sw.same else await right(report, sw.picked.value)
    return sw


async def evaluate(cfg: Config, rows: list[Row], embedder: Similarities | None) -> Evaluation:
    """Grade the keyword rule, then (when `embedder` answers) keywords plus embeddings and the sweep."""
    ev = Evaluation(keywords=await _score(cfg, rows, None, "Keywords only"))
    if cfg.embeddings is None:
        ev.embeddings_note = ("not graded: this config has no `embeddings:` block. To grade it, add one "
                              "(see config.example.yaml; `ollama pull nomic-embed-text`) and run again")
        return ev
    if embedder is None:
        ev.embeddings_note = "not graded: no embedder given"
        return ev
    memo = Memo(embedder)
    try:
        for r in rows:  # ask first, so a failure can't pass for a keyword-only answer inside the graded row
            await memo.similarities(request_text(r.prompt))
    except Exception as err:  # unreachable, a model that isn't pulled, an odd reply: all mean "not graded"
        ev.embeddings_note = (f"not graded: the embeddings provider {cfg.embeddings.provider!r} did not answer "
                              f"({type(err).__name__}). Start it (for Ollama: `ollama serve` and "
                              f"`ollama pull {cfg.embeddings.model}`) and run again")
        return ev
    if not memo.answered():
        ev.embeddings_note = ("not graded: no route in the config has `examples:`, so embeddings add nothing. "
                              "Add a few example prompts to each route and run again")
        return ev
    ev.embeddings = await _score(cfg, rows, memo, "Keywords + embeddings")
    ev.sweep = await _sweep(cfg, rows, memo)
    return ev


# ---------------------------------------------------------------- printing

def _pct(k: int, n: int) -> str:
    if n == 0:
        return "-"
    lo, hi = wilson(k, n)
    return f"{k}/{n} = {100 * k / n:.1f}%  (95% interval {100 * lo:.1f}-{100 * hi:.1f}%)"


def _confusion(cfg_routes: list[str], s: Score) -> list[str]:
    left = max(len(r) for r in cfg_routes)
    heads = list(cfg_routes)
    widths = [max(len(h), len(str(max(s.confusion(e, p) for e in cfg_routes)))) for h, p in zip(heads, cfg_routes, strict=True)]
    if 2 + left + sum(w + 2 for w in widths) > WIDTH:  # too many routes for names: number the columns
        heads = [str(i + 1) for i in range(len(cfg_routes))]
        labels = [f"{i + 1} {r}" for i, r in enumerate(cfg_routes)]
        left = max(len(x) for x in labels)
        widths = [max(len(h), 3) for h in heads]
    else:
        labels = list(cfg_routes)
    out = ["  " + " " * left + "".join(f"  {h:>{w}}" for h, w in zip(heads, widths, strict=True))]
    for lab, e in zip(labels, cfg_routes, strict=True):
        out.append("  " + f"{lab:<{left}}" + "".join(f"  {s.confusion(e, p):>{w}}" for p, w in zip(cfg_routes, widths, strict=True)))
    return out


def _score_lines(routes: list[str], s: Score) -> list[str]:
    left = max(len(r) for r in routes)
    out = [s.label, f"  accuracy {_pct(s.k, s.n)}", "  per route (of the rows labelled with it, how many it got right)"]
    for r in routes:
        k, n = s.per_route(r)
        out.append(f"    {r:<{left}}  {_pct(k, n) if n else 'no rows labelled with this route'}")
    out.append("  confusion (rows: expected route; columns: predicted route)")
    out += _confusion(routes, s)
    return out


def _sweep_lines(sw: Sweep) -> list[str]:
    out = ["Threshold sweep (embeddings.threshold; half the rows picks the value, the other half reports it)"]
    if sw.skipped:
        return out + [f"  skipped: {sw.skipped}"]
    out.append(f"  pick half: {sw.n_pick} rows; report half: {sw.n_report} rows (sorted by a hash of the prompt, cut in two)")
    for c in sw.candidates:
        tags = [x for x, on in (("current", c.current), ("picked", c is sw.picked)) if on]
        out.append(f"    {c.value:.2f}  {c.right:>3}/{sw.n_pick} right on the pick half" + (f"   <- {', '.join(tags)}" if tags else ""))
    if sw.same:
        out.append(f"  The current threshold {sw.current:.2f} is already the best on the pick half; nothing to compare.")
        return out
    n, a, b = sw.n_report, sw.only_current, sw.only_picked
    cur, pick = sum(sw.current_ok), sum(sw.picked_ok)
    out.append(f"  On the report half: current {sw.current:.2f}: {_pct(cur, n)}")
    out.append(f"                      picked  {sw.picked.value:.2f}: {_pct(pick, n)}")
    out.append(f"  The two settings disagree on {a + b} of the {n} report-half rows: current {sw.current:.2f} alone got {a} right, "
               f"picked {sw.picked.value:.2f} alone got {b} right.")
    if a + b == 0:
        out.append("  They agree on every row, so the report half cannot tell them apart.")
    else:
        out.append(f"  Exact sign test on those rows, two-sided: p = {sw.p:.3f}.")
        out.append(f"  p < {ALPHA}: a split this lopsided is unlikely to be chance." if sw.p < ALPHA
                   else f"  p >= {ALPHA}: the difference could be chance.")
    return out


def _fit(line: str) -> list[str]:
    """Wrap a prose line longer than the terminal width, indented under its own start; tables already fit."""
    if len(line) <= WIDTH:
        return [line]
    indent = " " * (len(line) - len(line.lstrip()))
    return textwrap.wrap(line, WIDTH, subsequent_indent=indent + "  ", break_long_words=False, break_on_hyphens=False)


def format_report(cfg: Config, ev: Evaluation, labels: str, n: int, config_path: str, today: str) -> list[str]:
    routes = list(cfg.routes)
    out = [f"Route accuracy, {today}", f"labels: {labels}", f"config: {config_path}", f"n = {n} labelled prompts",
           f"current embeddings.threshold: {cfg.embeddings.threshold:.2f}" if cfg.embeddings else "embeddings: not configured",
           "No chat model was called. Every number depends on this label set."]
    out += ["", *_score_lines(routes, ev.keywords), ""]
    if ev.embeddings:
        out += [*_score_lines(routes, ev.embeddings), ""]
    else:
        out += ["Keywords + embeddings", f"  {ev.embeddings_note}", ""]
    if ev.sweep:
        out += [*_sweep_lines(ev.sweep), ""]
    return out


# ---------------------------------------------------------------- the notebook proposal

def proposal_key(cfg: Config, rows: list[Row], sw: Sweep) -> str:
    """What the result depends on, in an order that does not: the labelled pairs (sorted), the embeddings model,
    each route's keywords, patterns and examples, and the two thresholds. Reordering the CSV keeps the key; editing
    a route's examples changes it."""
    picked = sw.picked.value if sw.picked else 0.0
    emb = cfg.embeddings
    basis = {
        "pairs": sorted([r.prompt, r.expected] for r in rows),
        "embeddings": [emb.provider, emb.model] if emb else None,
        "routes": {n: [list(r.keywords), [x.pattern for x in r.patterns], list(r.examples)] for n, r in sorted(cfg.routes.items())},
        "default": cfg.default_route,
    }
    digest = hashlib.sha256(json.dumps(basis, sort_keys=True, ensure_ascii=True).encode()).hexdigest()[:16]
    return f"route-threshold:{sw.current:.2f}>{picked:.2f}:{digest}"


def _known_keys(notebook: Path) -> set[str]:
    """Keys already in the notebook. Lines that are not JSON, or not JSON objects, are somebody else's: skipped."""
    try:
        text = notebook.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()
    keys = set()
    for line in text.splitlines():
        try:
            doc = json.loads(line)
        except ValueError:
            continue
        if isinstance(doc, dict) and isinstance(doc.get("key"), str):
            keys.add(doc["key"])
    return keys


def propose(cfg: Config, ev: Evaluation, rows: list[Row], labels: str, notebook: Path, dry_run: bool) -> str:
    """Record a better threshold in the notebook (once per set of labels, routes and thresholds) and say what happened."""
    sw = ev.sweep
    if sw is None or sw.skipped:
        return "Nothing written to the notebook: the sweep did not run."
    if sw.same:
        return "Nothing written to the notebook: the current threshold was already the best on the pick half."
    if not sw.better:
        return "Nothing written to the notebook: the picked threshold did not score higher than the current one on the report half."
    if not sw.proposes:
        return (f"Nothing written to the notebook: the picked threshold scored higher on the report half, but the difference "
                f"could be chance (p = {sw.p:.3f}, not below {ALPHA}).")
    key = proposal_key(cfg, rows, sw)
    n = sw.n_report
    finding = (f"embeddings.threshold {sw.current:.2f} -> {sw.picked.value:.2f} scored {sum(sw.picked_ok)}/{n} against "
               f"{sum(sw.current_ok)}/{n} on the report half of {Path(labels).name} ({len(rows)} labelled prompts; sign test "
               f"p = {sw.p:.3f}); review it, then set embeddings.threshold in your config")
    if dry_run:
        return f"Dry run: would add a proposal to {notebook} (nothing written): {finding}"
    if key in _known_keys(notebook):
        return f"Nothing written to the notebook: this proposal is already in {notebook}."
    line = json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "system": "trioditis", "kind": "route-threshold",
                       "subject": "embeddings.threshold", "decision": "proposal", "finding": finding, "key": key}) + "\n"
    try:
        _append_private(notebook, line)
    except OSError as err:
        return f"Could not write the proposal to {notebook}: {err.strerror or err}. Nothing was applied."
    return f"Proposal written to {notebook}. Nothing was applied to your config."


# ---------------------------------------------------------------- the command

def run(cfg: Config, args, *, embedder: Similarities | None = None,
        today: Callable[[], str] = lambda: dt.date.today().isoformat()) -> int:
    """`trioditis eval-routes`. `embedder` is for tests; normally the config's local provider is asked."""
    try:
        rows = read_labels(args.labels, cfg.routes)
    except LabelsError as err:
        sys.exit(f"eval-routes: {err}")

    async def go() -> Evaluation:
        if embedder is not None or cfg.embeddings is None:
            return await evaluate(cfg, rows, embedder)
        # Only the configured LOCAL embeddings provider is contacted: no redirects, no proxy.
        async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as client:
            return await evaluate(cfg, rows, Embedder(cfg, client))

    ev = asyncio.run(go())
    lines = format_report(cfg, ev, str(args.labels), len(rows), str(getattr(args, "config", "")), today())
    if ev.sweep is not None:
        book = Path(args.notebook).expanduser() if getattr(args, "notebook", None) else notebook_path()
        lines.append(propose(cfg, ev, rows, str(args.labels), book, bool(getattr(args, "dry_run", False))))
    print("\n".join(w for line in lines for w in _fit(line)).rstrip())
    return 0

