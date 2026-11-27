"""Command line entry point: python -m trioditis [init|serve|check|explain|eval-routes|results|hardware|usage|plan|prices|refit|reinforce|migrate]."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import stat
import sys
import textwrap
import time
from pathlib import Path

from . import __version__
from .config import ConfigError, load_config
from .envvars import env
from .security import assert_safe_bind

DEFAULT_PICKER = "qwen2.5-coder:7b"            # `results`: the picker the example config suggests
DEFAULT_LOCAL_URL = "http://localhost:11434/v1"

_ENV_LINE = re.compile(r"^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*?)\s*$")


def load_dotenv(path: str | Path = ".env") -> list[str]:
    """Minimal, dependency-free .env loader (same behaviour on Linux and Windows).

    Only KEY=VALUE lines; never overrides variables already set in the
    environment; values are not executed or expanded.
    """
    p = Path(path)
    if not p.is_file():
        return []
    if os.name == "posix" and p.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        print(f"warning: {p} is readable by other users; run: chmod 600 {p}", file=sys.stderr)
    loaded = []
    for line in p.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _ENV_LINE.match(line)
        if not m:
            continue
        key, value = m.group(1), m.group(2)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if value and key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded


def missing_config(path: str) -> str:
    """The first error a new user may see (`serve` outside the folder `init` made): say what to do."""
    where = Path(path).resolve()
    return (f"no config at {where}\n"
            f"  Trioditis reads config.yaml and .env from the current folder.\n"
            f"  First time? Run `trioditis init --dir ~/trioditis`, then `cd ~/trioditis`.\n"
            f"  Already set up? `cd` to that folder, or pass `--config /path/to/config.yaml` "
            f"(or set TRIODITIS_CONFIG).")


def build_parser() -> argparse.ArgumentParser:
    """Every command and flag. docs/compatibility.md lists each one with its label (tests/test_contract.py)."""
    ap = argparse.ArgumentParser(prog="trioditis")
    ap.add_argument("--config", default=env("CONFIG", "config.yaml"))
    ap.add_argument("--version", action="version", version=f"trioditis {__version__}")
    sub = ap.add_subparsers(dest="cmd")

    i = sub.add_parser("init", help="write config.yaml and .env from the templates (never overwrites)")
    i.add_argument("--dir", default=".", help="folder to set up (default: the current one)")

    s = sub.add_parser("serve", help="run the router (default)")
    s.add_argument("--host", default=env("HOST", "127.0.0.1"))
    s.add_argument("--port", type=int, default=int(env("PORT", "8787")))

    sub.add_parser("check", help="validate the config, ask local servers which models they have, show which cloud keys are set")

    e = sub.add_parser("explain", help="show which model a prompt would go to (no model call)")
    e.add_argument("prompt")

    ev = sub.add_parser("eval-routes", help="how often the router picks the right route, against a labelled CSV "
                                             "(no chat model is called); with --log, the route picker against the "
                                             "router on your own requests",
                        description="Grade the router's route choice on a labelled CSV: columns prompt and expected_route "
                                    "(UTF-8, header in row 1; other columns are ignored). Prints accuracy with a 95% interval, "
                                    "accuracy per route and a confusion table, for keywords alone and, when the config has "
                                    "embeddings and the local provider answers, keywords plus embeddings. With 20 or more rows it "
                                    "then picks embeddings.threshold on half the rows, scores it against yours on the other "
                                    "half (exact sign test) and records a proposal in the notebook only when the gain is "
                                    "unlikely to be chance. Changes nothing in the config. The only network call is to the "
                                    "configured local embeddings provider. With --log instead of --labels it grades nothing: "
                                    "it counts, from the decision log, how often the route picker and the router disagreed.")
    what = ev.add_mutually_exclusive_group(required=True)
    what.add_argument("--labels", metavar="FILE", help="the labelled prompts (CSV with prompt, expected_route)")
    what.add_argument("--log", nargs="?", const="", default=None, metavar="FILE",
                      help="instead of labels, read the decision log (default: the config's logging.path) and count, for "
                           "requests classified with `route_picker:` on, how often the picker answered, how often it and "
                           "the router disagreed and what the disagreements cost; reads files only, writes nothing")
    ev.add_argument("--config", default=argparse.SUPPRESS, metavar="FILE",
                    help="the config to grade (default: TRIODITIS_CONFIG or config.yaml; same as the option before the command)")
    ev.add_argument("--notebook", default=None, metavar="FILE", help="where a threshold proposal goes (default ~/.trioditis/trioditis-notebook.jsonl)")
    ev.add_argument("--dry-run", action="store_true", help="show the proposal that would be recorded, write nothing")
    ev.add_argument("--compare-threshold", type=float, default=None, metavar="VALUE",
                    help="instead of the sweep, score this embeddings.threshold against the config's on every row "
                         "(for a value chosen on other labels; nothing is proposed or written)")

    rs = sub.add_parser("results", help="measure the route picker and the router on this machine and write the numbers "
                                        "to a file you may share (local server only; no config needed)",
                        description="Run the route picker and the router on the public held-out set (315 labelled prompts "
                                    "shipped with Trioditis) with the shipped settings, time the picker by prompt length, and "
                                    "write the numbers to a JSON file, printed in full. Every prompt goes to the local server "
                                    "at --base-url and nowhere else; your config.yaml is not read. The file holds numbers, "
                                    "model names and digests, versions, a hardware summary and the date; never prompts, "
                                    "paths, user or host names. At the end it asks whether to share the numbers on GitHub "
                                    "(default No): yes opens the filled-in form in your browser and you press Submit.")
    rs.add_argument("--model", default=DEFAULT_PICKER, metavar="MODEL",
                    help="the picker model on the local server (default: %(default)s; pull it first with ollama pull)")
    rs.add_argument("--base-url", default=DEFAULT_LOCAL_URL, metavar="URL",
                    help="the local OpenAI-style server; must be on this machine (default: %(default)s)")
    rs.add_argument("--own-labels", default=None, metavar="FILE",
                    help="also run both on your own labelled prompts (CSV with prompt, expected_route using the routes "
                         "code, reasoning, writing, quick); only counts go in the file")
    rs.add_argument("--out", default="trioditis-results.json", metavar="FILE",
                    help="where the results go; an earlier file there is replaced (default: %(default)s)")
    rs.add_argument("--answers", default=None, metavar="FILE",
                    help="also write the route the picker named for each public held-out prompt, by row number, to compare "
                         "two models prompt by prompt (no prompt text; never your own labelled prompts; not shared; "
                         "default: not written)")
    rs.add_argument("--no-share", action="store_true", help="don't ask about sharing (it is never asked when not run in a terminal)")

    mg = sub.add_parser("migrate", help="move Trioditis's files from the old shared folder (~/.hlidskjalv) to "
                        "~/.trioditis, leaving a link at each old path; `serve` also does this when it starts "
                        "(no config needed)")
    mg.add_argument("--dry-run", action="store_true", help="show what would move, write nothing")

    h = sub.add_parser("hardware", help="detect hardware and suggest local models (no config change)")
    h.add_argument("--json", action="store_true", help="machine-readable output")

    u = sub.add_parser("usage", help="calls, tokens and spend from the metrics database (no config needed)")
    u.add_argument("--since", default="24h", help="30m, 24h, 7d, today, month or YYYY-MM-DD (default 24h)")
    u.add_argument("--until", default=None, help="same forms as --since; default now")
    u.add_argument("--by", default=None, help="model, route, provider, client, session or day (default model); "
                                               "with --plan: model-effort (default), model, effort, day, source, "
                                               "person or agent")
    u.add_argument("--session", default=None, help="only this X-Session-Id (matched by its hash)")
    u.add_argument("--db", default=None, help="metrics database (default ~/.trioditis/metrics.db)")
    u.add_argument("--outcomes", action="store_true",
                   help="rates with 95%% intervals instead of spend: errors, answers cut off at the length "
                        "limit, answers asked again within 2 minutes, prompt tokens served from cache")
    u.add_argument("--plan", action="store_true",
                   help="plan usage instead of API calls: messages and tokens per model and effort, and how far "
                        "each plan meter rose (fill it with `trioditis plan import`)")
    u.add_argument("--json", action="store_true", help="machine-readable output (always includes the rates)")

    pl = sub.add_parser("plan", help="plan usage: what a subscription's chats and agents used (no config needed)")
    pl_sub = pl.add_subparsers(dest="plan_cmd", required=True)
    pi = pl_sub.add_parser("import", help="import token counts and meter readings; with no arguments, what the "
                                          "config's plan: section names (the daily timer runs it that way)")
    pi.add_argument("file", nargs="?", help="a plan-usage.v1 events file")
    pi.add_argument("--claude-code", action="store_true",
                    help="token counts from Claude Code's transcripts (~/.claude, or $CLAUDE_CONFIG_DIR); no text is kept")
    pi.add_argument("--claude-home", default=None, help=argparse.SUPPRESS)
    pi.add_argument("--meter", nargs="?", const="", default=None, metavar="FILE",
                    help="plan meter readings (plan-meter.v1 or snapshots); without FILE, plan.meter_file")
    pi.add_argument("--db", default=None, help="metrics database (default ~/.trioditis/metrics.db)")
    pi.add_argument("--dry-run", action="store_true", help="show what would be imported, write nothing")

    pr = sub.add_parser("prices", help="manage ~/.trioditis/prices.json")
    pr_sub = pr.add_subparsers(dest="prices_cmd", required=True)
    imp = pr_sub.add_parser("import", help="take cloud model prices from a provider-facts file, with their sources")
    imp.add_argument("facts", help="a provider-facts.v1 JSON file (for example Euboulia's src/catalog/provider-facts.json)")
    imp.add_argument("--prices", default=None, help="prices file to update (default ~/.trioditis/prices.json)")
    imp.add_argument("--dry-run", action="store_true", help="show the changes, write nothing")

    rf = sub.add_parser("refit", help="refit rates, latency and drift charts from the call record, and check the "
                                      "model facts (changes nothing the router uses)")
    rf.add_argument("--days", type=int, default=28, help="window to fit, in days (default 28)")
    rf.add_argument("--db", default=None, help="metrics database (default ~/.trioditis/metrics.db)")
    rf.add_argument("--out", default=None, help="fitted file (default ~/.trioditis/trioditis-fitted.json)")
    rf.add_argument("--notebook", default=None, help="findings log (default ~/.trioditis/trioditis-notebook.jsonl)")
    rf.add_argument("--prices", default=None, help="prices file to compare (default ~/.trioditis/prices.json)")
    rf.add_argument("--json", action="store_true", help="print the fitted document")
    rf.add_argument("--dry-run", action="store_true", help="show the result, write nothing")

    rn = sub.add_parser("reinforce", help="the reinforcement loop: gate a routing change on your labelled prompts, "
                                          "or take an adopted one back (off unless the config has `reinforce:`)",
                        description="Routing values the loop adopts live in ~/.trioditis/trioditis-adopted.json, never "
                                    "in config.yaml, and only two can move: embeddings.threshold (inside 0.35 to 0.55, "
                                    "0.05 a step) and whether the route picker's route is served. A change has to pass "
                                    "the gate: right on more rows of your labelled gate set (config reinforce.labels) "
                                    "than the setting in force, with the 95% interval of the paired difference above "
                                    "-5 points. Network calls: the config's local embeddings provider and local route "
                                    "picker; nothing else.")
    rn_sub = rn.add_subparsers(dest="reinforce_cmd", required=True)
    rr = rn_sub.add_parser("run", help="gate each candidate on the labelled gate set and show the verdict "
                                       "(this version only with --dry-run: it adopts nothing yet)")
    rr.add_argument("--dry-run", action="store_true", help="show the verdict, write nothing")
    rv = rn_sub.add_parser("revert", help="take back the last adopted value (works with the loop on or off)")
    rv.add_argument("--all", action="store_true", help="take back every adopted value: back to the config.yaml values")
    rv.add_argument("--dry-run", action="store_true", help="show what would be taken back, write nothing")
    return ap


def main(argv: list[str] | None = None) -> None:
    load_dotenv(env("ENV_FILE", ".env"))  # before the parser: its defaults read TRIODITIS_* from .env too
    args = build_parser().parse_args(argv)
    if args.cmd == "usage":  # reads the metrics database only: works without a config
        sys.exit(plan_report(args) if args.plan else usage_report(args))
    if args.cmd == "plan":  # reads only the config's plan: section, and only when it needs it
        from .config import load_plan_settings
        from .plan import run as plan_run
        try:
            settings = load_plan_settings(args.config)
        except (OSError, ConfigError, ValueError) as err:
            sys.exit(f"config error in {args.config}: {err}")
        sys.exit(plan_run(args, settings))
    if args.cmd == "migrate":  # moves files in the data folder only: works without a config
        from .migrate import run as migrate_run
        sys.exit(migrate_run(args))
    if args.cmd == "init":  # creates the config: obviously works without one
        sys.exit(init_folder(Path(args.dir)))
    if args.cmd == "results":  # measures with the shipped settings: never reads config.yaml
        from .measure import run as results_run
        sys.exit(results_run(args))
    try:
        cfg = load_config(args.config)
    except FileNotFoundError:
        sys.exit(missing_config(args.config))
    except (OSError, ConfigError) as err:
        sys.exit(f"config error in {args.config}: {err}")

    if args.cmd == "check":
        print(f"config OK: {len(cfg.models)} models, {len(cfg.routes)} routes")
        from .localcheck import local_states
        # Local servers are asked once each (a model listing); cloud providers are never contacted.
        # Exit code: 0 whatever the states, as for a missing key: `check` reports, `serve` decides.
        states, notes = local_states(cfg)
        confirmed = 0
        for name, m in cfg.models.items():
            p = cfg.providers[m.provider]
            if not p.available():
                state = f"missing ${p.api_key_env}"
            elif p.local:
                state = states[name]
            else:
                state = "key set (not tested)"
            confirmed += state == "ready"
            where = "local" if p.local else "cloud"
            print(f"  {name:<24} {where:<6} {state}")
        for note in notes:
            print(textwrap.fill(note, width=98, initial_indent="  ", subsequent_indent="    ",
                                break_long_words=False, break_on_hyphens=False))
        print(f"{confirmed} of {len(cfg.models)} models confirmed ready; cloud providers are never contacted")
        from .localcheck import embeddings_lines
        for line in embeddings_lines(cfg):
            print(textwrap.fill(line, width=98, subsequent_indent="    ", break_long_words=False,
                                break_on_hyphens=False))
        from .localcheck import picker_lines
        for line in picker_lines(cfg):  # [] unless route_picker: is configured
            print(line)
        from .reinforce import check_lines
        for line in check_lines(cfg):  # [] unless `reinforce:` is configured or a value is adopted
            print(textwrap.fill(line, width=98, subsequent_indent="    ", break_long_words=False,
                                break_on_hyphens=False))
        for line in _facts_lines(cfg):
            print(line)
        from .migrate import check_lines as folder_lines
        for line in folder_lines():  # [] once the data folder has moved (or never needed to)
            print(textwrap.fill(line, width=98, subsequent_indent="    ", break_long_words=False,
                                break_on_hyphens=False))
        return

    if args.cmd == "reinforce":
        from . import reinforce
        sys.exit(reinforce.run(cfg, args) if args.reinforce_cmd == "run" else reinforce.revert(cfg, args))

    if args.cmd == "refit":
        from .refit import run
        sys.exit(run(cfg, args))

    if args.cmd == "prices":
        sys.exit(prices_import(cfg, args))

    if args.cmd == "explain":
        from trioditis_metrics.prices import Prices

        from .router import Signals, decide
        body = {"model": "auto", "messages": [{"role": "user", "content": args.prompt}]}
        # Keyword/rule signals only here, so the CLI never calls any network service; prices and past
        # latency (read-only) let it show a route's cost or speed order.
        from . import adopted
        try:
            doc = adopted.read() if cfg.reinforce else adopted.empty()
            note = None
        except adopted.AdoptedError as err:
            doc, note = adopted.empty(), f"adopted values not used: {err}"
        live = adopted.apply(cfg, doc)  # cfg itself unless `reinforce:` is on and a value is adopted
        d = asyncio.run(decide(live, body, signals=Signals(prices=Prices(), latency=_readonly_latency())))
        if note:
            d.reasons.append(note)
        d.reasons.extend(line for line in adopted.layer_lines(cfg, doc) if ": adopted " in line)
        if cfg.route_picker:
            d.reasons.append("route_picker is configured but not asked here (explain contacts no service); "
                             "POST /v1/route asks it")
        print(json.dumps(d.as_dict(), indent=2))
        return

    if args.cmd == "eval-routes":
        if args.log is not None:
            extra = [o for o, v in (("--notebook", args.notebook), ("--dry-run", args.dry_run),
                                    ("--compare-threshold", args.compare_threshold)) if v not in (None, False)]
            if extra:
                sys.exit(f"eval-routes: {', '.join(extra)} goes with --labels; --log only reads the decision log")
            from .evallog import run as eval_log
            sys.exit(eval_log(cfg, args))
        from .evalroutes import run as eval_routes
        sys.exit(eval_routes(cfg, args))

    if args.cmd == "hardware":
        import httpx

        from .hardware import build_report, detect, format_report, installed_models, load_prices

        async def listed():
            # Only local providers are queried (GET /api/tags); no proxy, no redirects.
            async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as c:
                return await installed_models(cfg, c)

        inst, errs = asyncio.run(listed())
        rep = build_report(cfg, detect(), inst, errs, load_prices())
        print(json.dumps(rep, indent=2) if args.json else format_report(rep))
        return

    host = getattr(args, "host", "127.0.0.1")
    port = getattr(args, "port", 8787)
    assert_safe_bind(host)

    if sys.stderr is None or sys.stdout is None:
        # Started without a console (pythonw.exe on Windows, some launchers):
        # send server output to a private log file instead of nowhere.
        Path("logs").mkdir(exist_ok=True)
        fd = os.open("logs/server.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        stream = os.fdopen(fd, "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = stream

    # Before anything opens the data folder: the modules below fix their default paths when imported.
    from .migrate import at_start
    at_start()

    for line in _facts_lines(cfg):
        print(f"warning: {line}", file=sys.stderr)

    import uvicorn

    from .app import create_app

    uvicorn.run(
        create_app(cfg),
        host=host,
        port=port,
        server_header=False,
        date_header=False,
        proxy_headers=False,
        log_level="info",
    )


def _facts_lines(cfg) -> list[str]:
    """What the model facts say about the configured cloud models; nothing when `facts:` is unset."""
    if not cfg.facts:
        return []
    from .pricefacts import check_lines
    cloud = {name: m.upstream_id for name, m in cfg.models.items() if not cfg.providers[m.provider].local}
    return check_lines(cfg.facts, cloud)


_AGO = re.compile(r"^(\d+)([mhdw])$")
_UNIT_S = {"m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}


def init_folder(folder: Path) -> int:
    """config.yaml and .env from the packaged templates; files that exist are left alone."""
    from importlib import resources
    folder = folder.expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    tpl = resources.files("trioditis") / "templates"
    wrote = []
    # Both files are private: .env holds keys, and config.yaml names your providers and local addresses.
    for name, target in (("config.example.yaml", "config.yaml"), ("env.example", ".env")):
        dest = folder / target
        if dest.exists():
            print(f"kept {dest} (already there)")
            continue
        data = tpl.joinpath(name).read_text(encoding="utf-8")
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        wrote.append(target)
        print(f"wrote {dest}")
    where = "" if folder.resolve() == Path.cwd().resolve() else f"cd {folder} && "
    print("next: " + (f"add cloud keys to {folder / '.env'} if you want them, then " if ".env" in wrote else "")
          + f"{where}trioditis check && trioditis serve   (config.yaml, .env and logs/ are read from that folder)")
    return 0


def _readonly_latency():
    import sqlite3

    from trioditis_metrics.db import DEFAULT_PATH, latency_p50, open_readonly

    def p50(model: str) -> int | None:
        try:
            conn = open_readonly(DEFAULT_PATH)
            try:
                return latency_p50(conn, model, time.time() - 7 * 86400)
            finally:
                conn.close()
        except sqlite3.Error:
            return None
    return p50


def parse_when(text: str, now: float | None = None) -> float:
    """'30m', '24h', '7d', '2w' ago; 'today' or 'month' (local midnight); 'YYYY-MM-DD' (local midnight)."""
    now = time.time() if now is None else now
    text = text.strip().lower()
    if m := _AGO.match(text):
        return now - int(m.group(1)) * _UNIT_S[m.group(2)]
    t = time.localtime(now)
    if text == "today":
        return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))
    if text == "month":
        return time.mktime((t.tm_year, t.tm_mon, 1, 0, 0, 0, 0, 0, -1))
    try:
        d = time.strptime(text, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"cannot read {text!r}: use 30m, 24h, 7d, today, month or YYYY-MM-DD") from None
    return time.mktime((d.tm_year, d.tm_mon, d.tm_mday, 0, 0, 0, 0, 0, -1))


def prices_import(cfg, args) -> int:
    from trioditis_metrics.prices import DEFAULT_PRICES_PATH

    from .pricefacts import FactsError, plan_import, stale_warning, write_prices

    try:
        facts = json.loads(Path(args.facts).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        print(f"prices import: cannot read {args.facts}: {err}", file=sys.stderr)
        return 2
    path = Path(args.prices).expanduser() if args.prices else DEFAULT_PRICES_PATH
    try:
        current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except ValueError:
        print(f"prices import: {path} is not valid JSON; fix or move it first", file=sys.stderr)
        return 2
    cloud = {name: m.upstream_id for name, m in cfg.models.items() if not cfg.providers[m.provider].local}
    try:
        table, changes, unmatched = plan_import(facts, cloud, current if isinstance(current, dict) else {})
    except FactsError as err:
        print(f"prices import: {err}", file=sys.stderr)
        return 2
    if warning := stale_warning(facts):
        print(f"warning: {warning}")
    for line in changes:
        print("  " + line)
    for name in unmatched:
        print(f"  {name} ({cloud[name]}): not in the facts file, left as it is")
    if not changes:
        print("nothing to import: no configured cloud model is in the facts file")
        return 1
    if args.dry_run:
        print(f"dry run: {path} not changed")
        return 0
    write_prices(path, table)
    print(f"wrote {path}")
    return 0


def usage_report(args) -> int:
    import sqlite3

    from trioditis_metrics.db import DEFAULT_PATH, GROUPS, open_readonly, outcome_rates, usage

    from .security import fingerprint

    try:
        since = parse_when(args.since)
        until = parse_when(args.until) if args.until else None
    except ValueError as err:
        print(f"usage: {err}", file=sys.stderr)
        return 2
    args.by = args.by or "model"
    if args.by not in GROUPS:
        print(f"usage: cannot group calls by {args.by!r}; choose from {', '.join(GROUPS)}", file=sys.stderr)
        return 2
    path = Path(args.db).expanduser() if args.db else DEFAULT_PATH
    sfp = fingerprint(args.session) if args.session else None
    try:
        conn = open_readonly(path)
        try:
            rows = usage(conn, since, until, args.by, sfp)
        finally:
            conn.close()
    except sqlite3.Error:
        rows = None  # no database yet (Trioditis has not served a call), or not one of ours
    total = {k: sum((r[k] or 0) for r in rows or []) for k in
             ("calls", "errors", "tokens_in", "tokens_out", "cost", "unpriced", "fallbacks", "local_calls",
              "finished", "truncated", "fingerprinted", "retried", "cached_tokens", "cache_base_tokens")}
    total["cost"] = round(total["cost"], 6)
    for r in [*(rows or []), total]:
        r["rates"] = outcome_rates(r)
    stamp = lambda ts: time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts)) if ts else None  # noqa: E731
    if args.json:
        for r in rows or []:
            r["first"], r["last"] = stamp(r.pop("first_ts")), stamp(r.pop("last_ts"))
        print(json.dumps({"since": stamp(since), "until": stamp(until), "by": args.by, "session": sfp,
                          "database": str(path), "rows": rows or [], "total": total}, indent=2))
        return 0
    if rows is None:
        print(f"no calls recorded yet (no metrics database at {path})")
        return 0
    span = f"since {stamp(since)}" + (f" until {stamp(until)}" if until else "")
    print(f"usage by {args.by} {span}" + (f", session {args.session!r}" if args.session else ""))
    if not rows:
        print("no calls in this window")
        return 0
    if args.outcomes:
        return _print_outcomes(args.by, rows, total)
    head = (args.by, "calls", "errors", "tokens in", "tokens out", "cost USD", "unpriced")
    table = [head] + [(str(r["key"]) if r["key"] is not None else "-", r["calls"], r["errors"], r["tokens_in"],
                       r["tokens_out"], f"{r['cost']:.4f}", r["unpriced"]) for r in rows]
    table.append(("total", total["calls"], total["errors"], total["tokens_in"], total["tokens_out"],
                  f"{total['cost']:.4f}", total["unpriced"]))
    _print_table(table)
    if total["unpriced"]:
        from trioditis_metrics.prices import DEFAULT_PRICES_PATH
        print(f"{total['unpriced']} cloud call(s) had no price: add them to {DEFAULT_PRICES_PATH}")
    return 0


def plan_report(args) -> int:
    import sqlite3

    from trioditis_metrics.db import DEFAULT_PATH, open_readonly
    from trioditis_metrics.plan import GROUPS, meter_rise, plan_usage

    by = args.by or "model-effort"
    if by not in GROUPS:
        print(f"usage --plan: cannot group by {by!r}; choose from {', '.join(GROUPS)}", file=sys.stderr)
        return 2
    try:
        since = parse_when(args.since)
        until = parse_when(args.until) if args.until else None
    except ValueError as err:
        print(f"usage: {err}", file=sys.stderr)
        return 2
    path = Path(args.db).expanduser() if args.db else DEFAULT_PATH
    try:
        conn = open_readonly(path)
        try:
            rows, meters = plan_usage(conn, since, until, by), meter_rise(conn, since, until)
        finally:
            conn.close()
    except sqlite3.Error:
        rows, meters = [], []
    keys = ("messages", "tokens_in", "cache_read", "cache_write", "tokens_out", "measured")
    total = {k: sum(r[k] or 0 for r in rows) for k in keys}
    stamp = lambda ts: time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts)) if ts else None  # noqa: E731
    if args.json:
        for r in rows:
            r["first"], r["last"] = stamp(r.pop("first_ts")), stamp(r.pop("last_ts"))
        print(json.dumps({"since": stamp(since), "until": stamp(until), "by": by, "database": str(path),
                          "rows": rows, "total": total, "meters": meters}, indent=2))
        return 0
    span = f"since {stamp(since)}" + (f" until {stamp(until)}" if until else "")
    print(f"plan usage by {by} {span}")
    if not rows:
        print("no plan usage in this window (import it with: trioditis plan import --claude-code)")
    else:
        head = (by, "messages", "in", "cache read", "cache write", "out", "measured")
        table = [head] + [(str(r["key"]), r["messages"], r["tokens_in"], r["cache_read"], r["cache_write"],
                           r["tokens_out"], f"{(r['measured'] or 0) / r['messages']:.0%}") for r in rows]
        table.append(("total", total["messages"], total["tokens_in"], total["cache_read"], total["cache_write"],
                      total["tokens_out"], f"{total['measured'] / total['messages']:.0%}"))
        _print_table(table)
        print("'in' is uncached prompt tokens; 'measured' is the share with real counts, the rest are estimates.")
    if meters:
        print("meters (whole percents; a reset counts the new window from zero):")
        for m in meters:
            name = m["meter"] + (f" ({m['model']})" if m["model"] else "")
            resets = f", {m['resets']} reset(s)" if m["resets"] else ""
            print(f"  {name:<28} +{m['rise']:g} points over {m['readings']:,} readings "
                  f"({m['first']:g}% -> {m['last']:g}%{resets})")
    else:
        print("no meter readings in this window (import them with: trioditis plan import --meter FILE)")
    return 0


def _pct(rate: dict) -> str:
    if rate["value"] is None:
        return "-"
    if "lo" not in rate:
        return f"{rate['value']:.0%}"
    return f"{rate['value']:.0%} ({rate['lo']:.0%}-{rate['hi']:.0%})"


def _print_outcomes(by: str, rows: list[dict], total: dict) -> int:
    head = (by, "calls", "errors", "cut off", "asked again", "cache hit")
    table = [head]
    for r in [*rows, {**total, "key": "total"}]:
        x = r["rates"]
        table.append((str(r["key"]) if r["key"] is not None else "-", r["calls"], _pct(x["error"]),
                      _pct(x["truncated"]), _pct(x["retried"]), _pct(x["cache_hit"])))
    _print_table(table)
    print("rates with 95% intervals; '-' means no data. 'cut off': of the answers whose provider said why they "
          "ended.\n'cache hit': of the prompt tokens whose provider reports cache use (Anthropic's "
          "OpenAI-compatible endpoint has no prompt caching).")
    return 0


def _print_table(table: list[tuple]) -> None:
    head = table[0]
    widths = [max(len(f"{row[i]:,}" if isinstance(row[i], int) else str(row[i])) for row in table)
              for i in range(len(head))]
    for n, row in enumerate(table):
        cells = [f"{v:,}" if isinstance(v, int) else str(v) for v in row]
        print("  ".join(c.ljust(widths[i]) if i == 0 else c.rjust(widths[i]) for i, c in enumerate(cells)))
        if n == 0 or n == len(table) - 2:
            print("  ".join("-" * w for w in widths))


if __name__ == "__main__":
    main()
