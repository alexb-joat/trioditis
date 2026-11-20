"""How long the route picker's call takes by prompt length (docs/route-accuracy.md). Not part of the
installed package.

    python scripts/picker_timing.py --model qwen2.5-coder:7b --out docs/route-accuracy/picker-timing.csv

Sends the request the router's picker sends (same instruction, temperature 0, seed 0, eight tokens),
with the routes of --config, N times per length in a shuffled order. The one network call is to
--base-url, which must be on this machine. The texts are slices of this repository's own docs at
random offsets, each used once, so no call repeats an earlier prompt; they are not written to the CSV.
Each run draws a new order and new offsets and prints its seed. --seed repeats a run's texts (while
those files are unchanged), but not its times: a server that has just seen the same texts answered many
of them in a fraction of the time (docs/route-accuracy/picker-timing-same-seed.csv), so measure with
texts the server has not seen. One untimed call first makes sure the model is loaded. The CSV gets one row per call as it
ends: characters, repeat, seconds, HTTP status, prompt tokens as the server counted them, and the
1-minute load average (the model's own threads are part of it).

This times the picker's call alone. In the router the embeddings call, when configured, runs at the
same moment and often on the same server, so leave headroom under timeout_ms when you set max_chars.
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import os
import random
import re
import statistics
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from trioditis.config import ConfigError, RoutePicker, load_config  # noqa: E402
from trioditis.router import PICKER_TEXT_CHARS, RoutePickerClient  # noqa: E402

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
TEXT_FILES = ["README.md", "CHANGELOG.md", "docs/route-accuracy.md"]


def _why(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        return f"HTTP {e.response.status_code} {e.response.text[:200].strip()}".rstrip(".")
    return f"{type(e).__name__} {e}".strip().rstrip(".")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--model", required=True, help="model id on the local server, e.g. qwen2.5-coder:7b")
    ap.add_argument("--config", default=str(ROOT / "config.example.yaml"), help="its routes make the instruction (default: %(default)s)")
    ap.add_argument("--base-url", default="http://localhost:11434/v1", help="local OpenAI-style server (default: %(default)s)")
    ap.add_argument("--lengths", default="100,150,200,250,300,500,750,1000,1500,2000,4000",
                    help="prompt lengths in characters, comma-separated (default: %(default)s)")
    ap.add_argument("--n", type=int, default=5, help="calls per length (default: %(default)s)")
    ap.add_argument("--seed", type=int, default=None,
                    help="order and text offsets (default: a new one each run, printed at the end; "
                         "repeating a seed resends texts the server may answer faster the second time)")
    ap.add_argument("--timeout-ms", type=int, default=RoutePicker.timeout_ms,
                    help="count calls slower than this (default: %(default)s, the picker's default)")
    ap.add_argument("--out", help="write one row per call to this CSV")
    a = ap.parse_args(argv)

    if urlsplit(a.base_url).hostname not in LOCAL_HOSTS:
        sys.exit(f"--base-url {a.base_url} is not on this machine; the texts are sent only to a local server "
                 f"(use one of: {', '.join(sorted(LOCAL_HOSTS))})")
    try:
        lengths = list(dict.fromkeys(int(x) for x in a.lengths.split(",")))
    except ValueError:
        sys.exit(f"--lengths must be whole numbers separated by commas (got {a.lengths!r})")
    if a.n < 1 or not lengths or min(lengths) < 1:
        sys.exit("--n and every length must be at least 1")
    if max(lengths) > PICKER_TEXT_CHARS:
        sys.exit(f"the longest length must be at most {PICKER_TEXT_CHARS:,} characters: the picker sends no more of a prompt than that")

    try:
        cfg = dataclasses.replace(load_config(a.config), route_picker=RoutePicker("local", a.model))
    except (OSError, ConfigError) as e:
        sys.exit(f"cannot use --config {a.config}: {e}")
    picker = RoutePickerClient(cfg, None)
    prose = re.sub(r"\s+", " ", " ".join((ROOT / f).read_text(encoding="utf-8") for f in TEXT_FILES))
    if max(lengths) * 2 > len(prose):
        sys.exit(f"the longest length must be under {len(prose) // 2:,} characters (half the text this script slices)")

    if a.seed is None:
        a.seed = int(time.time())
        print(f"seed {a.seed}", flush=True)  # now, so a run that is cut short still names its texts
    rng = random.Random(a.seed)  # noqa: S311  (a repeatable order and offsets, not a secret)
    jobs = [(chars, rep) for chars in lengths for rep in range(a.n)]
    if len(jobs) > len(prose) - 2 * max(lengths):
        sys.exit(f"{len(jobs):,} calls need more distinct texts than the docs give: lower --n or use fewer lengths")
    rng.shuffle(jobs)
    used: set[int] = set()
    rows: list[tuple] = []
    url = f"{a.base_url.rstrip('/')}/chat/completions"
    if a.out and not Path(a.out).resolve().parent.is_dir():  # before the run, not after it
        sys.exit(f"cannot write --out {a.out}: its folder does not exist")
    out = writer = None
    stopped = False
    # trust_env=False: a proxy variable must not carry the texts off this machine.
    with httpx.Client(timeout=300, trust_env=False) as client:
        try:
            client.post(url, json=picker.body("warm-up")).raise_for_status()
        except (httpx.HTTPError, httpx.InvalidURL) as e:
            sys.exit(f"no usable answer from {url} for model {a.model}: {_why(e)}. Is the server running and the model pulled?")
        if a.out:  # opened only now, so a failed start leaves an earlier file as it was
            try:
                out = open(a.out, "w", newline="", encoding="utf-8")  # noqa: SIM115  (closed below, after the loop)
            except OSError as e:
                sys.exit(f"cannot write --out {a.out}: {e.strerror}")
            writer = csv.writer(out)
            writer.writerow(["chars", "rep", "seconds", "status", "prompt_tokens", "load1"])
        for chars, rep in jobs:
            offset = rng.randrange(len(prose) - chars)
            while offset in used:
                offset = rng.randrange(len(prose) - chars)
            used.add(offset)
            t0 = time.monotonic()
            try:
                r = client.post(url, json=picker.body(prose[offset:offset + chars]))
            except httpx.HTTPError as e:
                print(f"stopped at {chars} characters, repeat {rep}: {_why(e)}. The {len(rows)} calls before it are kept.",
                      file=sys.stderr)
                stopped = True
                break
            seconds = time.monotonic() - t0
            try:
                tokens = r.json()["usage"]["prompt_tokens"]
            except (ValueError, KeyError, TypeError):
                tokens = ""
            rows.append((chars, rep, round(seconds, 3), r.status_code, tokens, round(os.getloadavg()[0], 2)))
            if writer:
                writer.writerow(rows[-1])
                out.flush()
            print(f"{chars:>6} chars  {seconds:6.2f} s  HTTP {r.status_code}", file=sys.stderr, flush=True)
    if out:
        out.close()

    limit = a.timeout_ms / 1000
    print(f"model {a.model}, {a.n} calls per length asked ({len(rows)} of {len(jobs)} made), seed {a.seed}, limit {limit:g} s")
    print(f"{'chars':>6} {'n':>3} {'median s':>9} {'min s':>7} {'max s':>7}  over {limit:g} s")
    for chars in lengths:
        xs = sorted(s for c, _, s, status, _, _ in rows if c == chars and status == 200)
        if not xs:
            print(f"{chars:>6}   0  (no successful call)")
            continue
        print(f"{chars:>6} {len(xs):>3} {statistics.median(xs):>9.2f} {xs[0]:>7.2f} {xs[-1]:>7.2f}  "
              f"{sum(x > limit for x in xs)}/{len(xs)}")
    failed = sum(1 for r in rows if r[3] != 200)
    if failed:
        print(f"{failed} call(s) did not return HTTP 200 and are left out of the table")
    if stopped or failed:
        sys.exit(1)  # the table above is incomplete


if __name__ == "__main__":
    main()
