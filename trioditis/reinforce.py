"""`trioditis reinforce`: the labelled gate a routing change has to pass, and the undo (docs/staying-current.md).

    trioditis reinforce run --dry-run     the gate's verdict on each candidate; writes nothing
    trioditis reinforce revert [--all]    take back the last adoption, or all of them

The gate scores a candidate setting against the setting in force on every row of the labelled gate set
(config `reinforce.labels`), the same rows for both. Network calls: the config's local embeddings
provider and its local route picker, once per row each. The gate set's prompts go nowhere else and are
never written anywhere.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx

from trioditis_metrics.stats import paired_difference, wilson

from . import adopted
from .adopted import PICKER_SERVES, THRESHOLD, AdoptedError
from .config import Config
from .evalroutes import LabelsError, Memo, Row, classify, read_labels, request_text
from .refit import _append_private, notebook_path
from .router import Embedder, PickerNoRoute, RoutePickerClient

MARGIN = 0.05           # the interval of candidate minus in-force must stay above minus this (5 points)
BATCH_CANDIDATES = 5    # a label batch decides this many candidates, then it is spent
OFF = ("the loop is off: this config has no `reinforce:` block. Add one with `labels:` (the labelled gate "
       "set, a CSV with prompt and expected_route) to turn it on; see docs/staying-current.md")


@dataclass(frozen=True)
class Verdict:
    """A candidate setting against another on the same rows."""
    n: int
    other_right: int
    candidate_right: int
    only_candidate: int
    only_other: int
    diff: float
    low: float
    high: float
    passed: bool
    why: str


def gate(other: list[bool], candidate: list[bool], margin: float = MARGIN) -> Verdict:
    """The gate's rule: the candidate passes only when it is right on more rows AND the 95% interval of
    the paired difference (candidate minus other, over every row) stays above minus `margin`."""
    if len(other) != len(candidate) or not other:
        raise ValueError("the gate needs the same rows, at least one, under both settings")
    n = len(other)
    b = sum(c and not o for o, c in zip(other, candidate, strict=True))
    c = sum(o and not c for o, c in zip(other, candidate, strict=True))
    diff, low, high = paired_difference(b, c, n)
    more = sum(candidate) > sum(other)
    if not more:
        passed, why = False, ("it is right on the same number of rows" if b == c else "it is right on fewer rows")
    elif low <= -margin:
        passed, why = False, (f"it is ahead, but the interval reaches {low * 100:+.1f} points, "
                              f"below the {-margin * 100:g}-point margin")
    else:
        passed, why = True, f"right on more rows and the interval stays above {-margin * 100:g} points"
    return Verdict(n, sum(other), sum(candidate), b, c, diff, low, high, passed, why)


# ---------------------------------------------------------------- what each row would be routed to

@dataclass
class Facts:
    """Per row: the router's own route at each threshold asked for, and the picker's route (None when
    the picker would not be asked or named no route). Routes only, no prompt text."""
    expected: list[str]
    router: dict[float, list[str]]
    picker: list[str | None]
    picker_asked: int = 0
    picker_no_route: int = 0

    def right(self, threshold: float | None, serves: bool) -> list[bool]:
        routes = self.router[threshold]
        return [(p if serves and p is not None else r) == e
                for r, p, e in zip(routes, self.picker, self.expected, strict=True)]


async def collect(cfg: Config, rows: list[Row], thresholds: list[float | None], embedder, picker) -> Facts:
    memo = Memo(embedder) if embedder is not None else None
    router: dict[float | None, list[str]] = {}
    for t in thresholds:
        c = cfg if t is None else dataclasses.replace(cfg, embeddings=dataclasses.replace(cfg.embeddings, threshold=t))
        router[t] = [await classify(c, r.prompt, memo) for r in rows]
    facts = Facts([r.expected for r in rows], router, [None] * len(rows))
    if picker is None:
        return facts
    p = cfg.route_picker
    for i, r in enumerate(rows):
        text = request_text(r.prompt)
        if not text.strip() or (len(text) > p.max_chars and p.long_prompts != "cut"):
            continue  # the running router would not ask the picker either
        facts.picker_asked += 1
        try:
            # With long_prompts: cut the running router gives it the first max_chars characters; so does this.
            facts.picker[i] = await picker.pick(text[:p.max_chars])  # waited for: the gate is about routes, not time
        except PickerNoRoute:
            facts.picker_no_route += 1
    return facts


# ---------------------------------------------------------------- one run

def gate_rows(cfg: Config) -> list[Row]:
    """The gate set's rows. A missing file gets this command's own advice, not eval-routes's."""
    if not Path(cfg.reinforce.labels).is_file():
        raise LabelsError(f"no gate set at {cfg.reinforce.labels} (config reinforce.labels); it is a CSV with the "
                          "columns prompt and expected_route, see docs/staying-current.md")
    return read_labels(cfg.reinforce.labels, cfg.routes)


def labels_sha(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def candidates(cfg: Config, doc: dict, sha: str) -> tuple[list[tuple[str, object]], list[str]]:
    """(candidates to gate, notes). This version puts one question to the labels, once per batch:
    should the picker's route be served? Threshold steps are proposed from traffic, which waits."""
    notes = []
    batch = doc.get("batch") if (doc.get("batch") or {}).get("sha256") == sha else None
    decided = (batch or {}).get("decided") or []
    out: list[tuple[str, object]] = []
    if cfg.route_picker is None:
        notes.append("route picker: no candidate (the config has no `route_picker:` block, so there is nothing "
                     "to compare the router with)")
    else:
        flip = not adopted.effective(cfg, doc["values"], PICKER_SERVES)
        taken_back = any(r.get("setting") == PICKER_SERVES and r.get("value") == flip and r.get("batch") == sha
                         for r in doc.get("reverted") or [])
        if len(decided) >= BATCH_CANDIDATES:
            notes.append(f"this label batch has decided {len(decided)} candidates and is spent: nothing is gated "
                         "until the gate set changes")
        elif any(d.get("setting") == PICKER_SERVES for d in decided):
            notes.append("route picker: no candidate (this label batch has already answered whether the picker serves)")
        elif taken_back:
            notes.append(f"route picker: no candidate (serves {adopted.show(flip)} was taken back; it is not "
                         "proposed again until the gate set changes)")
        else:
            out.append((PICKER_SERVES, flip))
    if cfg.embeddings is not None:
        notes.append("embeddings.threshold: no candidate (a step is proposed from traffic, which this version "
                     "does not read yet)")
    return out, notes


def _score(k: int, n: int) -> str:
    lo, hi = wilson(k, n)
    return f"{k} of {n} ({k / n * 100:.1f}%, 95% interval {lo * 100:.1f} to {hi * 100:.1f}%)"


def _verdict_lines(v: Verdict, against: str) -> list[str]:
    return [f"  against {against}: candidate right on {_score(v.candidate_right, v.n)}; "
            f"{against} on {_score(v.other_right, v.n)}",
            f"    only the candidate right on {v.only_candidate} row(s), only {against} on {v.only_other}; "
            f"difference {v.diff * 100:+.1f} points, 95% interval {v.low * 100:+.1f} to {v.high * 100:+.1f}",
            f"    {'passes' if v.passed else 'does not pass'}: {v.why}"]


def run(cfg: Config, args, *, embedder=None, picker=None, path: Path | None = None) -> int:
    """`trioditis reinforce run`. `embedder`, `picker` and `path` are for tests."""
    if cfg.reinforce is None:
        sys.exit(f"reinforce: {OFF}")
    if not args.dry_run:
        sys.exit("reinforce: adopting is not in this version: an adoption has to be able to revert itself when "
                 "its watch chart alarms, and the watch is not built yet. Run `trioditis reinforce run --dry-run` "
                 "to see the gate's verdict; it writes nothing")
    try:
        rows = gate_rows(cfg)
        sha = labels_sha(cfg.reinforce.labels)
        doc = adopted.read(path)
    except (LabelsError, AdoptedError) as err:
        sys.exit(f"reinforce: {err}")
    batch = doc.get("batch") if (doc.get("batch") or {}).get("sha256") == sha else None
    force = {s: adopted.effective(cfg, doc["values"], s) for s in adopted.SETTINGS}
    start = (batch or {}).get("baseline") or force  # the setting in force when this batch was first seen
    cands, notes = candidates(cfg, doc, sha)

    def setting(base: dict, change: tuple[str, object] | None = None) -> tuple[float | None, bool]:
        s = {**base, **({change[0]: change[1]} if change else {})}
        return s[THRESHOLD], bool(s[PICKER_SERVES])

    wanted = [setting(force), setting(start)] + [setting(force, c) for c in cands]
    thresholds = list(dict.fromkeys(t for t, _ in wanted))

    async def go() -> Facts:
        if embedder is not None or picker is not None:
            return await collect(cfg, rows, thresholds, embedder, picker)
        # Only the config's LOCAL embeddings provider and route picker are contacted: no redirects, no proxy.
        async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as client:
            return await collect(cfg, rows, thresholds, Embedder(cfg, client) if cfg.embeddings else None,
                                 RoutePickerClient(cfg, client) if cfg.route_picker and cands else None)

    try:
        facts = asyncio.run(go())
    except (httpx.HTTPError, OSError, ValueError, KeyError) as err:
        sys.exit(f"reinforce: a local model call failed ({type(err).__name__}: {err}); the gate needs the "
                 "embeddings provider and the route picker to answer. `trioditis check` shows which does not")

    print(f"gate set {cfg.reinforce.labels}: {len(rows)} rows, sha256 {sha[:16]}"
          + ("" if batch else " (a batch this loop has not gated yet)"))
    print("in force: " + "; ".join(adopted.layer_lines(cfg, doc)))
    now_right = facts.right(*setting(force))
    print(f"  right on {_score(sum(now_right), len(rows))}")
    would = None
    for c in cands:
        name = "route picker serves" if c[0] == PICKER_SERVES else c[0]
        print(f"candidate: {name} {adopted.show(c[1])}")
        if c[0] == PICKER_SERVES:
            p = cfg.route_picker
            others = ("empty; long_prompts: cut gave it the first max_chars characters of longer rows"
                      if p.long_prompts == "cut" else f"over max_chars {p.max_chars:,} or empty")
            print(f"  the picker was asked on {facts.picker_asked} of {len(rows)} rows (the others are {others}) "
                  f"and named no route on {facts.picker_no_route}")
        cand_right = facts.right(*setting(force, c))
        v = gate(now_right, cand_right)
        for line in _verdict_lines(v, "the setting in force"):
            print(line)
        ok = v.passed
        if setting(start) != setting(force):
            v0 = gate(facts.right(*setting(start)), cand_right)
            for line in _verdict_lines(v0, "the setting this batch started from"):
                print(line)
            ok = ok and v0.passed
        if ok and would is None:
            would = (name, c[1])
    for note in notes:
        print(note)
    today = adopted.now_iso()[:10]
    if would and adopted.last_change_day(doc) == today:
        print(f"would not adopt today: one change a day, and one was made on {today}")
    elif would:
        print(f"would adopt: {would[0]} {adopted.show(would[1])}")
    else:
        print("would adopt: nothing")
    print("dry run: nothing written")
    return 0


def revert(cfg: Config, args, *, path: Path | None = None, notebook: Path | None = None) -> int:
    """`trioditis reinforce revert`: works with the loop on or off, since it only takes back."""
    try:
        doc = adopted.read(path)
    except AdoptedError as err:
        sys.exit(f"reinforce: {err}")
    at = adopted.now_iso()
    new, entries = adopted.revert(cfg, doc, at=at, everything=args.all, reason="taken back by hand")
    if not entries:
        print("nothing to take back: no adopted value is in force (the config.yaml values serve)")
        return 0
    for e in entries:
        print(f"{e['setting']}: {adopted.show(e['before'])} back to {adopted.show(e['after'])} "
              f"(adopted {str(e['undoes'])[:10]})")
    if args.dry_run:
        print("dry run: nothing written")
        return 0
    adopted.write(new, path)
    book = notebook or notebook_path()
    _append_private(book, "".join(json.dumps({
        "ts": at, "system": "trioditis", "kind": "routing_revert", "setting": e["setting"], "before": e["before"],
        "after": e["after"], "undoes": e["undoes"], "reason": e["reason"],
        "finding": f"routing: {e['setting']} {adopted.show(e['before'])} taken back to {adopted.show(e['after'])} by hand",
        "key": f"routing_revert:{e['setting']}:{at}"}) + "\n" for e in entries))
    print(f"written to {path or adopted.adopted_path()}; the running router picks it up within a second or two. "
          f"Logged in {book}")
    return 0


def check_lines(cfg: Config, path: Path | None = None) -> list[str]:
    """What `trioditis check` says about the loop ([] when it is off and nothing is adopted)."""
    try:
        doc = adopted.read(path)
    except AdoptedError as err:
        return [f"reinforce: {err}"] if cfg.reinforce is not None else []
    if cfg.reinforce is None:
        held = ", ".join(doc["values"])
        return [f"reinforce: off (no `reinforce:` block), so the adopted value(s) for {held} are ignored and the "
                "config.yaml values serve"] if held else []
    labels = Path(cfg.reinforce.labels)
    try:
        found = f"{len(gate_rows(cfg))} rows"
    except LabelsError as err:
        found = f"NOT usable ({err})"
    lines = [f"reinforce: on; gate set {labels}: {found}"]
    lines += [f"  {line}" for line in adopted.layer_lines(cfg, doc)]
    if cfg.route_picker is None:
        lines.append("  no `route_picker:` block: the loop has nothing to compare the router with, so it proposes nothing")
    return lines
