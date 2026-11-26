"""Every TLS context the repo builds refuses TLS 1.0 and 1.1. `ssl.SSLContext(PROTOCOL_TLS_SERVER)` alone still
accepts them, and ruff's bandit rules (S502, S503, S504) do not flag that: CodeQL did (py/insecure-protocol). So
each `SSLContext(...)` call must sit in a function that also sets `.minimum_version` to TLS 1.2 or 1.3. The
negative cases run the same check on in-memory copies of the console-check stand-in."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".venv", "venv", ".claude", ".git", "build", "dist", "node_modules", "__pycache__"}
FLOORS = {"TLSv1_2", "TLSv1_3"}
STAND_IN = ROOT / "scripts" / "console_check.py"


def python_files(root: Path = ROOT) -> list[Path]:
    out = []
    for path in sorted(root.rglob("*.py")):
        parts = path.relative_to(root).parts[:-1]
        if any(p in SKIP_DIRS or p.endswith(".egg-info") for p in parts):
            continue
        out.append(path)
    return out


def _called_name(call: ast.Call) -> str:
    f = call.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return ""


def _sets_floor(scope: ast.AST) -> bool:
    for node in ast.walk(scope):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        if any(isinstance(t, ast.Attribute) and t.attr == "minimum_version" for t in targets):
            name = value.attr if isinstance(value, ast.Attribute) else getattr(value, "id", "")
            if name in FLOORS:
                return True
    return False


def unfloored_contexts(source: str, filename: str = "<memory>") -> list[str]:
    """`file:line` of each SSLContext(...) call whose enclosing function (or module) never sets a TLS 1.2+ floor."""
    tree = ast.parse(source, filename=filename)
    functions = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    misses = []
    for call in (n for n in ast.walk(tree) if isinstance(n, ast.Call) and _called_name(n) == "SSLContext"):
        # the innermost function holding the call; the module if it is at top level
        holders = [f for f in functions if f.lineno <= call.lineno <= (f.end_lineno or f.lineno)]
        holder = max(holders, key=lambda f: f.lineno) if holders else tree
        if not _sets_floor(holder):
            misses.append(f"{filename}:{call.lineno}")
    return misses


def test_every_ssl_context_sets_a_tls12_floor():
    misses = []
    for path in python_files():
        misses += unfloored_contexts(path.read_text(encoding="utf-8"), path.relative_to(ROOT).as_posix())
    assert not misses, f"SSLContext without `.minimum_version = ssl.TLSVersion.TLSv1_2` (or 1_3): {misses}"


def test_the_stand_in_is_scanned_and_has_a_context():
    assert STAND_IN in python_files()
    assert "SSLContext(" in STAND_IN.read_text(encoding="utf-8")


def test_removing_the_floor_from_the_stand_in_fails():
    lines = STAND_IN.read_text(encoding="utf-8").splitlines(keepends=True)
    kept = [line for line in lines if ".minimum_version" not in line]
    assert len(kept) == len(lines) - 1, "expected exactly one floor line in the stand-in"
    assert unfloored_contexts("".join(kept), "console_check.py"), "the check passed with the floor removed"


def test_a_tls10_floor_does_not_count():
    source = STAND_IN.read_text(encoding="utf-8").replace("TLSVersion.TLSv1_2", "TLSVersion.TLSv1")
    assert unfloored_contexts(source, "console_check.py")
