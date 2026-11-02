"""Prices from a provider-facts file instead of typed by hand.

A provider-facts file (schema "provider-facts.v1", the one Euboulia's catalog is built from) lists,
per provider, each model's API id and price with the documentation page it was read from. This module
matches it against config.yaml by the model id sent upstream and writes ~/.hlidskjalv/prices.json
entries that carry their source:

    {"claude-haiku": {"input": 0.1, "output": 0.5,
                      "long_context": {"threshold": 100000, "input": 0.5, "output": 2.5},
                      "_source": "https://…/pricing", "_fetched": "2026-10-14"}}
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

SCHEMA = "provider-facts.v1"
STALE_DAYS = 30


class FactsError(ValueError):
    pass


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0


def facts_by_api_id(facts: dict) -> dict[str, dict]:
    """{api id: {"input", "output", "long_context"?, "_source", "_fetched"}} for every priced model."""
    if not isinstance(facts, dict) or facts.get("$schema") != SCHEMA:
        raise FactsError(f"not a {SCHEMA} file")
    out: dict[str, dict] = {}
    for prov in (facts.get("providers") or {}).values():
        sources = prov.get("sources") or {}
        for m in (prov.get("models") or {}).values():
            api_id, price = m.get("apiId"), m.get("price") or {}
            if not isinstance(api_id, str) or not (_num(price.get("input")) and _num(price.get("output"))):
                continue
            src = sources.get((m.get("sources") or {}).get("price")) or {}
            entry = {"input": price["input"], "output": price["output"]}
            tier = price.get("longContext")
            if isinstance(tier, dict) and all(_num(tier.get(k)) for k in ("threshold", "input", "output")):
                entry["long_context"] = {k: tier[k] for k in ("threshold", "input", "output")}
            entry["_source"] = src.get("url")
            entry["_fetched"] = src.get("fetched")
            out[api_id] = entry
    return out


def same_price(old: dict | None, new: dict) -> bool:
    return old is not None and all(old.get(k) == new.get(k) for k in ("input", "output", "long_context"))


def plan_import(facts: dict, cloud_models: dict[str, str], current: dict) -> tuple[dict, list[str], list[str]]:
    """cloud_models: {config name: upstream id}. Returns (new prices table, change lines, unmatched names)."""
    known = facts_by_api_id(facts)
    table = dict(current)
    changes, unmatched = [], []
    for name, upstream_id in sorted(cloud_models.items()):
        new = known.get(upstream_id)
        if new is None:
            unmatched.append(name)
            continue
        old = current.get(name) if isinstance(current.get(name), dict) else None
        same = same_price(old, new)
        table[name] = new
        before = f"${old.get('input')}/${old.get('output')}" if old else "no price"
        tier = (f", ${new['long_context']['input']}/${new['long_context']['output']} above "
                f"{new['long_context']['threshold']:,} input tokens") if "long_context" in new else ""
        verb = "unchanged" if same else f"{before} -> ${new['input']}/${new['output']}{tier}"
        changes.append(f"{name} ({upstream_id}): {verb} per 1M tokens")
    return table, changes, unmatched


def stale_warning(facts: dict, today: dt.date | None = None) -> str | None:
    try:
        made = dt.date.fromisoformat(str(facts.get("generatedAt"))[:10])
    except ValueError:
        return "the facts file has no generatedAt date; check its prices against the provider's page"
    age = ((today or dt.date.today()) - made).days
    if age > STALE_DAYS:
        return f"the facts are {age} days old (from {made}); prices may have changed since"
    return None


def retirement(text) -> tuple[dt.date, bool] | None:
    """(date, firm) from a facts `retiresAt`: "2026-11-19" is a set date; "not before 2027-06-09"
    only the earliest one (Anthropic's wording for models without a set date). Anything else: None."""
    if not isinstance(text, str):
        return None
    t = text.strip().lower()
    firm = not t.startswith("not before")
    try:
        return dt.date.fromisoformat(t.removeprefix("not before").strip()[:10]), firm
    except ValueError:
        return None


def lifecycle_by_api_id(facts: dict) -> dict[str, dict]:
    """{api id: {"provider", "name", "status", "retires", "note", "price", "_source", "_fetched"}}
    for every model the facts list, priced or not. The source is the page the status (or, failing
    that, the retirement date) was read from."""
    if not isinstance(facts, dict) or facts.get("$schema") != SCHEMA:
        raise FactsError(f"not a {SCHEMA} file")
    out: dict[str, dict] = {}
    for pname, prov in (facts.get("providers") or {}).items():
        sources = prov.get("sources") or {}
        for m in (prov.get("models") or {}).values():
            api_id = m.get("apiId")
            if not isinstance(api_id, str):
                continue
            refs = m.get("sources") or {}
            src = sources.get(refs.get("status") or refs.get("retiresAt")) or {}
            price = m.get("price") or {}
            out[api_id] = {
                "provider": pname, "name": m.get("name"), "status": m.get("status"),
                "retires": m.get("retiresAt"), "note": m.get("note"),
                "price": {"input": price["input"], "output": price["output"]}
                if _num(price.get("input")) and _num(price.get("output")) else None,
                "_source": src.get("url"), "_fetched": src.get("fetched"),
            }
    return out


def lifecycle_report(facts: dict, configured: dict[str, str], today: dt.date | None = None,
                     horizon_days: int = 30) -> dict:
    """What the facts say about the configured cloud models ({config name: upstream id}).

    warnings: a model listed as deprecated or retired, or retiring (or allowed to retire) within
    `horizon_days`. Each quotes the fact, its source page and the facts' own note, because a
    status can be narrower than it looks (OpenAI's "retired" may mean retired from ChatGPT only).
    unknown: configured models the facts don't list, so nothing can be said about them.
    challengers: active models of the same providers that the config doesn't use yet. Listed to
    be tried, never added.
    """
    today = today or dt.date.today()
    known = lifecycle_by_api_id(facts)
    warnings, unknown, in_use = [], [], set()
    for name, upstream_id in sorted(configured.items()):
        f = known.get(upstream_id)
        if f is None:
            unknown.append({"model": name, "api_id": upstream_id})
            continue
        in_use.add(f["provider"])
        why = []
        if f["status"] in ("deprecated", "retired"):
            why.append(f"listed as {f['status']}")
        when = retirement(f["retires"])
        if when and when[0] <= today + dt.timedelta(days=horizon_days):
            date, firm = when
            if firm:
                why.append(f"{'retired' if date <= today else 'retires'} on {date}")
            elif f["status"] != "active":
                why.append(f"may retire from {date} (the provider says not before)")
        if why:
            warnings.append({"model": name, "api_id": upstream_id, "why": "; ".join(why), "status": f["status"],
                             "retires": f["retires"], "note": f["note"], "source": f["_source"],
                             "fetched": f["_fetched"]})
    used_ids = set(configured.values())
    challengers = [{"provider": f["provider"], "api_id": api_id, "name": f["name"], "price": f["price"]}
                   for api_id, f in sorted(known.items())
                   if f["provider"] in in_use and f["status"] == "active" and api_id not in used_ids]
    return {"warnings": warnings, "unknown": unknown, "challengers": challengers}


def lifecycle_lines(report: dict) -> list[str]:
    """The report as lines for `check`, the startup log and `refit`."""
    lines = []
    for w in report["warnings"]:
        line = f"{w['model']} ({w['api_id']}): {w['why']}"
        if w.get("note"):
            line += f"; note: {w['note']}"
        if w.get("source"):
            line += f" [{w['source']}, {w['fetched']}]"
        lines.append(line)
    for u in report["unknown"]:
        lines.append(f"{u['model']} ({u['api_id']}): not in the facts file, so its status is unknown")
    return lines


def check_lines(path: str, configured: dict[str, str], today: dt.date | None = None) -> list[str]:
    """Warnings about the configured cloud models from the facts file at `path`, for `check` and
    the startup log. Never raises: an unreadable file is itself the warning."""
    try:
        facts = json.loads(Path(path).read_text(encoding="utf-8"))
        report = lifecycle_report(facts, configured, today)
    except (OSError, ValueError) as err:  # FactsError is a ValueError
        return [f"model facts: cannot use {path}: {err}"]
    lines = [f"model facts: {line}" for line in lifecycle_lines(report)]
    if stale := stale_warning(facts, today):
        lines.append(f"model facts: {stale}")
    return lines


def write_prices(path: Path, table: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(table, indent=2) + "\n")
    os.replace(tmp, path)
