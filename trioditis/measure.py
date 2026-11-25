"""`trioditis results`: measure the route picker and the router on this machine, as numbers you may share.

What it runs, on a LOCAL model server only (the same host rule as `eval-routes`, proxy variables ignored):

1. The route picker's waiting time by prompt length: the picker's own request (`RoutePickerClient.body`) at a
   few lengths, three calls each, after one untimed call that loads the model. The texts are slices of the
   held-out prompts at random offsets, sent before the accuracy run so the server has not seen them.
2. The picker on the public held-out set (315 prompts shipped with the package, labels fixed in advance).
3. The router at the shipped defaults on the same set: keywords alone, and keywords plus embeddings at the
   shipped threshold when the embeddings model answers. Fixed settings only: nothing is swept or tuned on
   this set (docs/route-accuracy.md, "The held-out test").
4. With `--own-labels FILE`: the router and the picker on your labelled prompts, reported as counts only.

Every setting comes from the example config shipped in the package, never from your config.yaml, so results
from different machines measure the same thing. The one thing you choose is the picker model.

The results file holds numbers, model names and digests, versions, the hardware summary and the date. It never
holds prompt text, file paths, user or host names, fingerprints or times finer than the date
(`tests/test_measure.py` checks this with marker strings). Sharing is a question at the end, default No; Yes
opens GitHub's form in your browser with the numbers filled in, and you press Submit. This program sends
nothing anywhere except to the local server.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import hashlib
import json
import platform
import random
import statistics
import sys
import time
import webbrowser
from collections import Counter
from collections.abc import Callable
from importlib import resources
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import httpx

from trioditis_metrics.stats import sign_test, wilson

from . import __version__
from .config import Config, ConfigError, RoutePicker, load_config
from .evalroutes import LabelsError, Row, classify, read_labels, request_text
from .hardware import Hardware, detect
from .router import PICKER_TEXT_CHARS, Embedder, PickerNoRoute, RoutePickerClient

SCHEMA = "trioditis-results.v1"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
PROVIDER = "ollama"                    # the example config's local provider
TIMING_LENGTHS = (100, 200, 500, 1000, 2000)
TIMING_REPEATS = 3
FIRST_CALL_S = 300.0                   # the untimed first call may load the model from disk
FORM_URL = "https://github.com/alexb-joat/trioditis/issues/new"
FORM_TEMPLATE = "results.yml"
FORM_FIELD = "results"                 # the form's field id (.github/ISSUE_TEMPLATE/results.yml)
MAX_URL = 8000                         # my cautious cap, not a documented GitHub limit: past it, paste the file
NO_ROUTE = "-"                         # a reply that names no route, or a call that failed: counted as wrong


class MeasureError(Exception):
    """Stops the run; the message names the cause and the fix."""


# ---------------------------------------------------------------- what is measured

def shipped_config_path() -> Path:
    return Path(str(resources.files("trioditis") / "templates" / "config.example.yaml"))


def heldout_path() -> Path:
    return Path(str(resources.files("trioditis") / "data" / "routes-heldout.csv"))


def require_local(url: str, what: str = "--base-url") -> None:
    """Refuse any server that is not on this machine: every prompt in the run is sent there."""
    try:
        host = urlsplit(url).hostname
    except ValueError:
        host = None
    if host not in LOCAL_HOSTS:
        raise MeasureError(f"{what} {url} is not on this machine; the prompts are sent only to a local server "
                           f"(use one of: {', '.join(sorted(LOCAL_HOSTS))})")


def measured_config(model: str, base_url: str, config_path: Path | None = None) -> Config:
    """The shipped example config with the picker set to `model` and the local provider at `base_url`."""
    require_local(base_url)
    path = config_path or shipped_config_path()
    try:
        cfg = load_config(path)
    except (OSError, ConfigError) as err:  # only if the installed package is damaged
        raise MeasureError(f"cannot read the shipped config {path}: {err}; reinstall trioditis") from err
    if PROVIDER not in cfg.providers or cfg.embeddings is None:
        raise MeasureError(f"the shipped config {path} has no {PROVIDER!r} provider or embeddings block; reinstall trioditis")
    prov = dataclasses.replace(cfg.providers[PROVIDER], base_url=base_url.rstrip("/"))
    return dataclasses.replace(cfg, providers={**cfg.providers, PROVIDER: prov},
                               route_picker=RoutePicker(PROVIDER, model))


def _short_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _rate(k: int, n: int) -> dict:
    lo, hi = wilson(k, n) or (None, None)
    return {"right": k, "n": n, "low95": None if lo is None else round(lo, 4), "high95": None if hi is None else round(hi, 4)}


def score(routes: list[str], rows: list[Row], picked: list[str]) -> dict:
    """Counts for one setting: right of n with a 95% interval, per route, and a confusion table."""
    k = sum(r.expected == p for r, p in zip(rows, picked, strict=True))
    out = _rate(k, len(rows))
    out["per_route"] = {route: _rate(sum(r.expected == p == route for r, p in zip(rows, picked, strict=True)),
                                     sum(r.expected == route for r in rows)) for route in routes}
    table = Counter((r.expected, p) for r, p in zip(rows, picked, strict=True))
    out["confusion"] = {e: {p: table[(e, p)] for p in [*routes, NO_ROUTE] if table[(e, p)]} for e in routes}
    return out


def paired(rows: list[Row], first: list[str], second: list[str]) -> dict:
    """Rows only the first setting got right, only the second, and the exact sign test between them."""
    b = sum(p == r.expected != q for r, p, q in zip(rows, first, second, strict=True))
    c = sum(q == r.expected != p for r, p, q in zip(rows, first, second, strict=True))
    return {"only_first": b, "only_second": c, "sign_test_p": round(sign_test(b, c), 6)}


def machine(hw: Hardware) -> dict:
    """The hardware summary that goes in the file: no host name, no user name, no paths, no probe notes."""
    return {"os": hw.os, "arch": hw.arch, "python": platform.python_version(), "cpu": hw.cpu, "cores": hw.cores,
            "ram_gb": None if hw.ram_total is None else round(hw.ram_total / 1e9, 1),
            "unified_memory": hw.unified_memory,
            "gpus": [{"name": g.name, "vendor": g.vendor, "shared": g.shared,
                      "vram_gb": None if g.vram_bytes is None else round(g.vram_bytes / 1e9, 1)} for g in hw.gpus]}


def timing_texts(rows: list[Row], rng: random.Random) -> list[tuple[int, str]]:
    """(length, text) pairs: slices of the held-out prompts joined end to end, each from a different offset,
    in a shuffled order."""
    prose = " ".join(" ".join(r.prompt.split()) for r in rows)
    longest = max(TIMING_LENGTHS)
    if longest > PICKER_TEXT_CHARS or 2 * longest > len(prose):
        raise MeasureError(f"the timing texts need at least {2 * longest} characters of prompts; the held-out set "
                           "is damaged, reinstall trioditis")
    jobs = [n for n in TIMING_LENGTHS for _ in range(TIMING_REPEATS)]
    rng.shuffle(jobs)
    used: set[int] = set()
    out = []
    for n in jobs:
        offset = rng.randrange(len(prose) - n)
        while offset in used:
            offset = rng.randrange(len(prose) - n)
        used.add(offset)
        out.append((n, prose[offset:offset + n]))
    return out


def _seconds(xs: list[float]) -> dict:
    xs = sorted(xs)
    if not xs:
        return {"n": 0}
    p90 = xs[min(len(xs) - 1, round(0.9 * (len(xs) - 1)))]
    return {"n": len(xs), "median": round(statistics.median(xs), 2), "p90": round(p90, 2),
            "min": round(xs[0], 2), "max": round(xs[-1], 2)}


# ---------------------------------------------------------------- talking to the local server

def _root(base_url: str) -> str | None:
    """Ollama's own API sits beside its OpenAI-style `/v1`; None when the server isn't shaped like that."""
    base = base_url.rstrip("/")
    return base[:-3] if base.endswith("/v1") else None


async def server_facts(client: httpx.AsyncClient, cfg: Config) -> dict:
    """Ollama's version and the digests of the two models, when the server is Ollama; nulls otherwise."""
    root = _root(cfg.providers[PROVIDER].base_url)
    facts: dict = {"server": None, "version": None, "picker_digest": None, "embeddings_digest": None}
    if root is None:
        return facts
    try:
        r = await client.get(f"{root}/api/version", timeout=5)
        r.raise_for_status()
        facts["server"], facts["version"] = "ollama", str(r.json().get("version") or "") or None
        r = await client.get(f"{root}/api/tags", timeout=5)
        r.raise_for_status()
        digests = {str(m.get(k, "")): m.get("digest") for m in r.json().get("models", []) for k in ("name", "model")}
    except (httpx.HTTPError, ValueError, AttributeError, TypeError):
        return facts
    for key, model in (("picker_digest", cfg.route_picker.model), ("embeddings_digest", cfg.embeddings.model)):
        facts[key] = digests.get(model) or digests.get(f"{model}:latest")
    return facts


def _is_chat_completion(r: httpx.Response) -> bool:
    try:
        r.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        return False
    return True


async def _first_call(client: httpx.AsyncClient, cfg: Config, picker: RoutePickerClient) -> None:
    url = f"{cfg.providers[PROVIDER].base_url}/chat/completions"
    model = cfg.route_picker.model
    try:
        r = await client.post(url, json=picker.body("Hello, how are you today?"), timeout=FIRST_CALL_S)
        r.raise_for_status()
    except httpx.HTTPStatusError as err:
        code = err.response.status_code
        if code == 404:
            raise MeasureError(f"{url} answered HTTP 404 for model {model!r}. Is it pulled? Run: ollama pull {model}") from None
        raise MeasureError(f"{url} answered HTTP {code} for model {model!r}: the server could not run it. Read the "
                           "server's own log for the reason, or try another model with --model") from None
    except httpx.HTTPError as err:
        raise MeasureError(f"no answer from {url} ({type(err).__name__}). Start the server (for Ollama: ollama serve) "
                           "or pass its address with --base-url") from None
    if not _is_chat_completion(r):  # else every prompt would count as "named no route" in a file that looks like a measurement
        raise MeasureError(f"{url} answered HTTP 200 but not with a chat completion. Is --base-url an OpenAI-style "
                           "server (for Ollama: http://localhost:11434/v1)?")


async def time_picker(client: httpx.AsyncClient, cfg: Config, picker: RoutePickerClient,
                      texts: list[tuple[int, str]], clock: Callable[[], float], say: Callable[[str], None]) -> dict:
    url = f"{cfg.providers[PROVIDER].base_url}/chat/completions"
    times: dict[int, list[float]] = {n: [] for n in TIMING_LENGTHS}
    failed = 0
    for i, (n, text) in enumerate(texts, 1):
        t0 = clock()
        try:
            r = await client.post(url, json=picker.body(text), timeout=FIRST_CALL_S)
            r.raise_for_status()
        except httpx.HTTPError:
            failed += 1
            continue
        times[n].append(clock() - t0)
        say(f"  timing {i}/{len(texts)}")
    return {"repeats": TIMING_REPEATS, "failed": failed,
            "by_chars": [{"chars": n, **_seconds(xs)} for n, xs in times.items()]}


async def pick_all(picker: RoutePickerClient, prompts: list[str], clock: Callable[[], float],
                   say: Callable[[str], None], label: str) -> tuple[list[str], dict]:
    """The picker's route for each prompt (NO_ROUTE for no route or a failed call, which is retried once)."""
    picked, seconds, no_route, failed = [], [], 0, 0
    for i, prompt in enumerate(prompts, 1):
        for attempt in (1, 2):
            t0 = clock()
            try:
                route = await picker.pick(request_text(prompt))
                seconds.append(clock() - t0)
                break
            except PickerNoRoute:
                seconds.append(clock() - t0)
                route, no_route = NO_ROUTE, no_route + 1
                break
            except httpx.HTTPError:
                if attempt == 2:
                    route, failed = NO_ROUTE, failed + 1
        picked.append(route)
        if i % 25 == 0 or i == len(prompts):
            say(f"  {label} {i}/{len(prompts)}")
    return picked, {"no_route": no_route, "failed_after_retry": failed, "seconds_per_call": _seconds(seconds)}


def require_answered(calls: dict, n: int, what: str) -> None:
    """Stops a run whose picker calls mostly failed: counted as wrong, they would make a file that looks like a
    measurement of the model and is one of the server dropping out. (More than half is my line, not a standard.)"""
    failed = calls["failed_after_retry"]
    if 2 * failed > n:
        raise MeasureError(f"{failed} of {n} picker calls on {what} failed even after a retry, so these numbers would not "
                           "measure the picker. Nothing was written and nothing is offered for sharing. The server "
                           "probably stopped answering: check that it is still running, then run this again")


async def route_all(cfg: Config, prompts: list[str], embedder) -> list[str]:
    return [await classify(cfg, p, embedder) or NO_ROUTE for p in prompts]


async def embedder_or_reason(cfg: Config, client: httpx.AsyncClient, prompts: list[str]) -> tuple[Embedder | None, str | None]:
    """The embedder, checked on every prompt first so a failure can't pass for a keyword-only answer."""
    emb = Embedder(cfg, client)
    try:
        for p in prompts:
            await emb.similarities(request_text(p))
    except Exception as err:  # unreachable, not pulled, an odd reply: all mean "not graded"
        return None, (f"not graded: {cfg.embeddings.model} did not answer ({type(err).__name__}); "
                      f"run: ollama pull {cfg.embeddings.model}")
    return emb, None


class _Remembering:
    """Asks the embedder once per text; `classify` asks again for the same texts."""

    def __init__(self, inner: Embedder):
        self.inner, self.seen = inner, {}

    async def similarities(self, text: str) -> dict[str, float]:
        if text not in self.seen:
            self.seen[text] = await self.inner.similarities(text)
        return self.seen[text]


async def measure(cfg: Config, heldout: list[Row], own: list[Row] | None, *, transport=None,
                  clock: Callable[[], float] = time.monotonic, rng: random.Random | None = None,
                  say: Callable[[str], None] = lambda s: print(s, file=sys.stderr, flush=True)) -> dict:
    routes = list(cfg.routes)
    rng = rng or random.Random()  # noqa: S311  (offsets for timing texts, not a secret)
    # No proxy, no redirects: the prompts go to the local server and nowhere else.
    async with httpx.AsyncClient(follow_redirects=False, trust_env=False, transport=transport) as client:
        picker = RoutePickerClient(cfg, client)
        facts = await server_facts(client, cfg)
        say(f"loading {cfg.route_picker.model} (the first call may take a while)")
        await _first_call(client, cfg, picker)
        say("timing the picker by prompt length")
        timing = await time_picker(client, cfg, picker, timing_texts(heldout, rng), clock, say)

        prompts = [r.prompt for r in heldout]
        say(f"the picker on {len(prompts)} held-out prompts")
        picked, calls = await pick_all(picker, prompts, clock, say, "picker")
        require_answered(calls, len(prompts), "the held-out prompts")
        keywords = await route_all(cfg, prompts, None)
        say(f"the router with {cfg.embeddings.model}")
        embedder, note = await embedder_or_reason(cfg, client, prompts + [r.prompt for r in own or []])
        memo = _Remembering(embedder) if embedder else None
        routed = await route_all(cfg, prompts, memo) if memo else None

        result_heldout = {
            "n": len(heldout),
            "picker": {**score(routes, heldout, picked), **calls},
            "router_keywords": score(routes, heldout, keywords),
            "router": score(routes, heldout, routed) if routed else None,
            "router_note": note,
            "picker_vs_router": paired(heldout, picked, routed or keywords) | {"router": "router" if routed else "router_keywords"},
        }
        result_own = None
        if own:
            say(f"your {len(own)} labelled prompts")
            own_prompts = [r.prompt for r in own]
            own_picked, own_calls = await pick_all(picker, own_prompts, clock, say, "your prompts")
            require_answered(own_calls, len(own_prompts), "your prompts")
            own_routed = await route_all(cfg, own_prompts, memo)
            result_own = {"n": len(own),
                          "picker": {**score(routes, own, own_picked), "no_route": own_calls["no_route"],
                                     "failed_after_retry": own_calls["failed_after_retry"]},
                          "router": score(routes, own, own_routed),
                          "router_uses_embeddings": memo is not None,
                          "picker_vs_router": paired(own, own_picked, own_routed)}
    return {"timing": timing, "heldout": result_heldout, "own_labels": result_own, "server": facts}


# ---------------------------------------------------------------- the file and the form

def results_doc(cfg: Config, measured: dict, hw: Hardware, today: str, config_bytes: bytes, labels_bytes: bytes) -> dict:
    picker = RoutePickerClient(cfg, None)
    facts = measured["server"]
    return {
        "schema": SCHEMA,
        "trioditis": __version__,
        "date": today,
        "settings": {
            "config_sha256_16": _short_hash(config_bytes),
            "heldout_sha256_16": _short_hash(labels_bytes),
            "instruction_sha256_16": _short_hash(picker.system().encode("utf-8")),
            "embeddings_threshold": cfg.embeddings.threshold,
            "routes": list(cfg.routes),
        },
        "models": {
            "server": facts["server"], "server_version": facts["version"],
            "picker": cfg.route_picker.model, "picker_digest": facts["picker_digest"],
            "embeddings": cfg.embeddings.model, "embeddings_digest": facts["embeddings_digest"],
        },
        "machine": machine(hw),
        "timing": measured["timing"],
        "heldout": measured["heldout"],
        "own_labels": measured["own_labels"],
    }


def form_url(doc: dict) -> str | None:
    """GitHub's results form with the numbers filled in, or None when the link would be too long."""
    title = f"Results: {doc['models']['picker']} on {doc['machine']['os']} {doc['machine']['arch']}"
    url = f"{FORM_URL}?" + urlencode({"template": FORM_TEMPLATE, "title": title,
                                      FORM_FIELD: json.dumps(doc, separators=(",", ":"))})
    return url if len(url) <= MAX_URL else None


def summary(doc: dict) -> list[str]:
    h = doc["heldout"]
    def line(name: str, s: dict | None) -> str:
        if s is None:
            return f"  {name:<18} {h['router_note']}"
        return f"  {name:<18} {s['right']}/{s['n']} = {100 * s['right'] / s['n']:.1f}%  (95% interval {100 * s['low95']:.1f}-{100 * s['high95']:.1f}%)"
    pv = h["picker_vs_router"]
    out = [f"Held-out set, {h['n']} prompts (labels fixed in advance; fixed settings, nothing tuned on them)",
           line(f"picker {doc['models']['picker']}", h["picker"]),
           line("router", h["router"]), line("router, keywords", h["router_keywords"]),
           f"  picker vs {pv['router']}: only the picker right {pv['only_first']}, only the router right {pv['only_second']}, "
           f"exact sign test p = {pv['sign_test_p']:.3g}",
           "Picker waiting time on this machine, warm (seconds: median / slowest)"]
    out += [f"  {t['chars']:>5} characters  " + (f"{t['median']:.2f} / {t['max']:.2f}  (n = {t['n']})" if t["n"] else "no successful call")
            for t in doc["timing"]["by_chars"]]
    timing_calls = doc["timing"]["repeats"] * len(doc["timing"]["by_chars"])
    dropped = [f"{k} of {n} {what}" for k, n, what in
               ((h["picker"]["failed_after_retry"], h["n"], "picker calls on the held-out set failed even after a retry "
                                                           "and count as wrong"),
                (doc["timing"]["failed"], timing_calls, "timing calls failed and are left out of the times")) if k]
    if dropped:
        out.append("Note: " + "; ".join(dropped) + " (the server dropped or refused them)")
    if doc["own_labels"]:
        o = doc["own_labels"]
        out.append(f"Your labelled prompts, {o['n']}: picker {o['picker']['right']}, router {o['router']['right']} right "
                   "(counts only; your prompts are not in the file)")
    return out


def ask_to_share(doc: dict, *, ask: Callable[[str], str] = input, opener: Callable[[str], bool] = webbrowser.open,
                 say: Callable[[str], None] = print, out_name: str = "trioditis-results.json") -> bool:
    """The one question. Returns True only when the browser was asked to open the filled-in form."""
    try:
        answer = ask("Share these numbers publicly on GitHub? They go into a public issue under CC0. [y/N] ")
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer.strip().lower() not in ("y", "yes"):
        say("Not shared. Nothing was sent anywhere.")
        return False
    url = form_url(doc)
    if url is None:
        say(f"The numbers are too long for a link. Open {FORM_URL}?template={FORM_TEMPLATE} and paste the "
            f"contents of {out_name} into the Results box.")
        return False
    if opener(url):
        say("Opened GitHub's form in your browser with the numbers filled in. Read them, then press Submit; "
            "nothing is sent until you do.")
        return True
    say(f"Could not open a browser. Open this link, read the numbers, then press Submit:\n{url}")
    return False


def run(args, *, transport=None, hardware: Callable[[], Hardware] = detect, isatty: Callable[[], bool] | None = None,
        ask: Callable[[str], str] = input, opener: Callable[[str], bool] = webbrowser.open,
        today: Callable[[], str] = lambda: dt.date.today().isoformat()) -> int:
    """`trioditis results`."""
    try:
        cfg = measured_config(args.model, args.base_url)
        labels = heldout_path()
        heldout = read_labels(labels, cfg.routes)
        if args.own_labels and not Path(args.own_labels).is_file():  # read_labels would name `--labels`, a flag of eval-routes
            raise MeasureError(f"--own-labels {args.own_labels} is not a file; pass a CSV with the columns "
                               "prompt and expected_route")
        own = read_labels(args.own_labels, cfg.routes) if args.own_labels else None
    except (MeasureError, LabelsError) as err:
        sys.exit(f"results: {err}" + (" (your labels must use the shipped routes)" if isinstance(err, LabelsError) and args.own_labels else ""))
    out = Path(args.out)
    if out.is_dir():  # found out now, not after a run of an hour or more
        sys.exit(f"results: --out {args.out} is a folder; give it a file name")
    if not out.resolve().parent.is_dir():
        sys.exit(f"results: cannot write --out {args.out}: its folder does not exist; create it or pass another --out")
    try:
        measured = asyncio.run(measure(cfg, heldout, own, transport=transport))
    except MeasureError as err:
        sys.exit(f"results: {err}")
    doc = results_doc(cfg, measured, hardware(), today(), shipped_config_path().read_bytes(), labels.read_bytes())
    text = json.dumps(doc, indent=2) + "\n"
    try:
        out.write_text(text, encoding="utf-8")
    except OSError as err:  # the run may have taken hours: the numbers go to the screen before the error
        print(text, end="")
        sys.exit(f"results: cannot write --out {args.out}: {err.strerror or err}. The whole file is printed above; "
                 "copy it before you close this window")
    print(text, end="")
    print("\n".join(summary(doc)))
    print(f"Wrote {args.out}: the whole file is printed above.")
    interactive = isatty() if isatty else (sys.stdin.isatty() and sys.stdout.isatty())
    if not args.no_share and interactive:
        ask_to_share(doc, ask=ask, opener=opener, out_name=args.out)
    return 0
