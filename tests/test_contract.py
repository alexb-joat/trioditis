"""The 1.0 contract: every public item the code has is documented with its label, and nothing documented is gone.

docs/compatibility.md lists commands and flags, environment variables, HTTP endpoints, headers, the metrics database
and the decision log; docs/configuration.md lists every config.yaml key. Each list here is read from the code, not
typed in, so a new flag, key, variable, endpoint, header or column fails this file until its row is written.
"""

from __future__ import annotations

import argparse
import ast
import re
import sqlite3
from pathlib import Path

import yaml

from trioditis.__main__ import build_parser
from trioditis.config import CONFIG_KEYS, parse_config

ROOT = Path(__file__).resolve().parent.parent
COMPAT = ROOT / "docs" / "compatibility.md"
CONFIG_DOC = ROOT / "docs" / "configuration.md"
SOURCES = sorted([*(ROOT / "trioditis").glob("*.py"), *(ROOT / "trioditis_metrics").glob("*.py")])
LABELS = {"stable", "experimental", "internal"}


# ---- the docs side ----------------------------------------------------------------------------------------------

def tables(path: Path) -> dict[str, dict[str, str]]:
    """Rows of every table, by the `## ` heading above it: {heading: {item: label}}. A row counts when its first
    cell is one code span (the item) and its second cell starts with the label."""
    out: dict[str, dict[str, str]] = {}
    heading = ""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            heading = line[3:].strip()
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")] if line.lstrip().startswith("|") else []
        if len(cells) < 2 or not re.fullmatch(r"`[^`]+`", cells[0]):
            continue
        label = re.sub(r"[*_]", "", cells[1]).split(" ")[0].lower()
        out.setdefault(heading, {})[cells[0].strip("`")] = label
    return out


def section(path: Path, heading: str) -> dict[str, str]:
    rows = tables(path).get(heading)
    assert rows, f"{path.name} has no table under '## {heading}'"
    return rows


# ---- the code side ----------------------------------------------------------------------------------------------

def cli_items() -> set[str]:
    """`trioditis <command> [<subcommand>]` and `... --flag` / `... <positional>` for every argparse entry."""
    items: set[str] = set()

    def walk(parser: argparse.ArgumentParser, prefix: str) -> None:
        for action in parser._actions:
            if isinstance(action, argparse._HelpAction):
                continue
            if isinstance(action, argparse._SubParsersAction):
                for name, child in action.choices.items():
                    items.add(f"{prefix} {name}")
                    walk(child, f"{prefix} {name}")
            elif action.option_strings:
                items.add(f"{prefix} {max(action.option_strings, key=len)}")
            else:
                items.add(f"{prefix} <{action.dest}>")

    walk(build_parser(), "trioditis")
    return items


def env_items() -> set[str]:
    """TRIODITIS_<NAME> for each env("NAME") call, and every literal os.environ.get / os.getenv name."""
    names: set[str] = set()
    for path in SOURCES:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                continue
            f, arg = node.func, node.args[0].value
            if isinstance(f, ast.Name) and f.id == "env":
                names.add("TRIODITIS_" + arg)
            elif isinstance(f, ast.Attribute) and (
                    (f.attr == "get" and isinstance(f.value, ast.Attribute) and f.value.attr == "environ")
                    or f.attr == "getenv"):
                names.add(arg)
    return names


def _minimal_config():
    return parse_config({"providers": {"local": {"base_url": "http://127.0.0.1:11434/v1", "local": True}},
                         "models": {"m": {"provider": "local"}},
                         "routes": {"quick": {"models": ["m"]}}, "default_route": "quick",
                         "logging": {"path": None}})


def endpoint_items(tmp_path: Path) -> set[str]:
    from fastapi.routing import APIRoute

    from trioditis.app import create_app
    from trioditis_metrics.db import MetricsDB
    from trioditis_metrics.guard import BudgetCaps, BudgetGuard
    from trioditis_metrics.prices import Prices

    metrics = MetricsDB(tmp_path / "metrics.db")
    app = create_app(_minimal_config(), metrics=metrics, budget=BudgetGuard(metrics, BudgetCaps()),
                     prices=Prices(tmp_path / "prices.json"), hardware_file=tmp_path / "hardware.json",
                     accounts_file=tmp_path / "accounts.json")
    return {f"{method} {route.path}" for route in app.routes if isinstance(route, APIRoute)
            for method in route.methods if method not in ("HEAD", "OPTIONS")}


def header_items() -> set[str]:
    found: set[str] = set()
    for path in SOURCES:
        for m in re.finditer(r"x-trioditis-[a-z]+(?:-[a-z]+)*", path.read_text(encoding="utf-8"), re.IGNORECASE):
            found.add("-".join(part.capitalize() for part in m.group(0).split("-")))
    return found


def db_items(tmp_path: Path) -> set[str]:
    from trioditis_metrics.db import MetricsDB

    MetricsDB(tmp_path / "metrics.db")
    con = sqlite3.connect(tmp_path / "metrics.db")
    try:
        tables_ = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
        return {f"{t}.{col[1]}" for t in tables_ for col in con.execute(f"PRAGMA table_info({t})")}  # noqa: S608
    finally:
        con.close()


def decision_log_items() -> set[str]:
    """The keys `_log` in app.py writes to decisions.jsonl: the record's literal keys and every rec["..."] = ..."""
    tree = ast.parse((ROOT / "trioditis" / "app.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_log")
    keys: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "rec" \
                and isinstance(node.value, ast.Dict):
            keys |= {k.value for k in node.value.keys if isinstance(k, ast.Constant)}
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == "rec" \
                and isinstance(node.ctx, ast.Store) and isinstance(node.slice, ast.Constant):
            keys.add(node.slice.value)
    return keys


def config_items() -> set[str]:
    return {f"{sec}.{key}" if sec else key for sec, keys in CONFIG_KEYS.items() for key in keys}


# ---- the contract -----------------------------------------------------------------------------------------------

def check(rows: dict[str, str], code: set[str], what: str) -> None:
    missing, gone = sorted(code - set(rows)), sorted(set(rows) - code)
    bad = sorted(f"{item}: {label!r}" for item, label in rows.items() if label not in LABELS)
    assert not missing, f"{what} in the code but not documented (add a row with its label): {missing}"
    assert not gone, f"{what} documented but not in the code (remove the row, or deprecate first): {gone}"
    assert not bad, f"{what} with a label that is not stable, experimental or internal: {bad}"


def test_every_command_and_flag_is_documented():
    check(section(COMPAT, "Commands and flags"), cli_items(), "commands and flags")


def test_every_environment_variable_is_documented():
    check(section(COMPAT, "Environment variables"), env_items(), "environment variables")


def test_every_http_endpoint_is_documented(tmp_path):
    check(section(COMPAT, "HTTP endpoints"), endpoint_items(tmp_path), "HTTP endpoints")


def test_every_trioditis_header_is_documented():
    rows = section(COMPAT, "Headers")
    trioditis_rows = {k: v for k, v in rows.items() if k.lower().startswith("x-trioditis-")}
    check(trioditis_rows, header_items(), "X-Trioditis-* headers")
    assert all(label in LABELS for label in rows.values())


def test_every_metrics_column_is_documented(tmp_path):
    check(section(COMPAT, "Metrics database"), db_items(tmp_path), "metrics.db columns")


def test_every_decision_log_field_is_documented():
    check(section(COMPAT, "Decision log"), decision_log_items(), "decisions.jsonl fields")


def test_every_config_key_is_documented():
    rows = {item: label for heading in tables(CONFIG_DOC).values() for item, label in heading.items()}
    check(rows, config_items(), "config keys")


def test_every_row_on_both_pages_has_a_label():
    for page in (COMPAT, CONFIG_DOC):
        bad = [f"{heading}: {item} = {label!r}" for heading, rows in tables(page).items()
               for item, label in rows.items() if label not in LABELS]
        assert not bad, f"{page.name}: rows whose label is not stable, experimental or internal: {bad}"


def test_the_loader_reads_no_key_outside_config_keys():
    """CONFIG_KEYS is typed by hand: every literal key a parse function reads must be in it."""
    tree = ast.parse((ROOT / "trioditis" / "config.py").read_text(encoding="utf-8"))
    parsers = {"parse_config", "_parse_route_picker", "_parse_reinforce", "parse_plan"}
    known = set().union(*CONFIG_KEYS.values())
    read: set[str] = set()
    for fn in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name in parsers):
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get" \
                    and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                read.add(node.args[0].value)
            elif isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load) \
                    and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
                read.add(node.slice.value)
    assert read, "found no key reads: has the loader moved?"
    assert read <= known, f"read by the loader but missing from CONFIG_KEYS: {sorted(read - known)}"


def test_the_example_config_uses_only_known_keys():
    raw = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    found: set[str] = set(raw)
    for sec in CONFIG_KEYS:
        if not sec:
            continue
        top, _, named = sec.partition(".")
        blocks = (raw.get(top) or {}).values() if named else [raw.get(top) or {}]
        for block in blocks:
            found |= {f"{sec}.{k}" for k in (block if isinstance(block, dict) else {})}
    assert found <= config_items(), f"config.example.yaml uses keys the loader ignores: {sorted(found - config_items())}"


def test_the_contract_pages_are_linked():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/compatibility.md" in readme and "docs/configuration.md" in readme
