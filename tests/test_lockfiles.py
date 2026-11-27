"""The two lockfiles stay what their own header commands make. CI installs them with `--require-hashes --no-deps`,
so a package missing from a lockfile is never noticed by pip; these checks notice. Each check is a function over the
file's text, so it can be pointed at any version of a lockfile."""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RUNTIME = REPO / "requirements.txt"
DEV = REPO / "requirements-dev.txt"
DEV_IN = REPO / "requirements-dev.in"

# One package entry: `name==version [; marker]` and its continuation lines (hashes), up to the next entry.
ENTRY = re.compile(r"(?m)^([A-Za-z0-9][A-Za-z0-9_.-]*)==([^\s;\\]+)([^\\\n]*)((?:\s*\\\n\s+--hash=sha256:[0-9a-f]{64})*)")
HASH = re.compile(r"--hash=sha256:([0-9a-f]{64})")
HEADER = re.compile(r"^#\s+uv pip compile (\S+\.in) .*--generate-hashes.*-o (\S+\.txt)\s*$")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def entries(text: str) -> dict[str, tuple[str, str, frozenset[str]]]:
    """name (lowercased) -> (version, marker, hashes)."""
    out = {}
    for name, version, marker, hashes in ENTRY.findall(text):
        out[name.lower()] = (version, marker.strip().lstrip(";").strip(), frozenset(HASH.findall(hashes)))
    return out


def runtime_mismatches(runtime: str, dev: str) -> list[str]:
    """Every runtime pin must appear in the dev lockfile with the same version and hashes (dev compiles with
    `-c requirements.txt`)."""
    dev_entries = entries(dev)
    problems = []
    for name, (version, _marker, hashes) in entries(runtime).items():
        if name not in dev_entries:
            problems.append(f"{name}=={version} is in requirements.txt but not in requirements-dev.txt")
            continue
        dev_version, _dev_marker, dev_hashes = dev_entries[name]
        if dev_version != version:
            problems.append(f"{name}: {version} in requirements.txt, {dev_version} in requirements-dev.txt")
        elif dev_hashes != hashes:
            problems.append(f"{name}=={version}: the hashes differ between the two lockfiles")
    return problems


def windows_colorama_missing(dev_in: str, dev: str) -> str | None:
    """pytest needs colorama on Windows. `uv pip compile --universal` keeps it as a win32-only line; Dependabot's
    lockfile edits have dropped it (PRs #16, #20, #21) and once dropped every runtime package (PR #12). Windows CI
    still passed, because `--no-deps` never asks for colorama, so only this check sees the drift."""
    if not re.search(r"(?m)^pytest\b", dev_in):
        return None
    for name, (_version, marker, _hashes) in entries(dev).items():
        if name == "colorama" and re.fullmatch(r"sys_platform\s*==\s*['\"]win32['\"]", marker):
            return None
    return "requirements-dev.txt has no `colorama==... ; sys_platform == 'win32'` line though pytest is a dev input"


def header_and_hash_problems(text: str, lockfile: str, source: str) -> list[str]:
    """The first lines name the `uv pip compile` command that built the file; every package line carries a hash."""
    problems = []
    head = text.splitlines()[:3]
    commands = [m for m in (HEADER.match(line) for line in head) if m]
    if not commands:
        problems.append(f"{lockfile}: the first lines do not name a `uv pip compile ... --generate-hashes` command")
    elif (commands[0].group(1), commands[0].group(2)) != (source, lockfile):
        problems.append(f"{lockfile}: the header compiles {commands[0].group(1)} -o {commands[0].group(2)}")
    pins = re.findall(r"(?m)^[A-Za-z0-9][A-Za-z0-9_.-]*==", text)
    unhashed = [name for name, (_v, _m, hashes) in entries(text).items() if not hashes]
    if len(pins) != len(entries(text)):
        problems.append(f"{lockfile}: {len(pins)} pins but {len(entries(text))} parsed entries")
    if unhashed:
        problems.append(f"{lockfile}: no --hash=sha256 for {unhashed}")
    return problems


def test_parser_reads_every_pin():
    for path in (RUNTIME, DEV):
        assert len(entries(read(path))) == len(re.findall(r"(?m)^[A-Za-z0-9][A-Za-z0-9_.-]*==", read(path))) > 0


def test_runtime_pins_match_the_dev_lockfile():
    assert runtime_mismatches(read(RUNTIME), read(DEV)) == []


def test_dev_lockfile_keeps_windows_colorama():
    assert windows_colorama_missing(read(DEV_IN), read(DEV)) is None


def test_headers_name_their_command_and_every_pin_is_hashed():
    assert header_and_hash_problems(read(RUNTIME), "requirements.txt", "requirements.in") == []
    assert header_and_hash_problems(read(DEV), "requirements-dev.txt", "requirements-dev.in") == []
    for name in ("requirements-audit", "requirements-release"):   # the tools CI and the release build install
        assert header_and_hash_problems(read(REPO / f"{name}.txt"), f"{name}.txt", f"{name}.in") == []


def test_dropping_colorama_fails():
    dev = read(DEV)
    trimmed = re.sub(r"(?ms)^colorama==.*?(?=^[A-Za-z0-9])", "", dev)
    assert trimmed != dev
    assert windows_colorama_missing(read(DEV_IN), trimmed)


def test_a_moved_runtime_pin_fails():
    runtime = read(RUNTIME)
    name, (version, _m, _h) = next(iter(entries(runtime).items()))
    moved = re.sub(rf"(?m)^{re.escape(name)}=={re.escape(version)}", f"{name}==0.0.0", read(DEV), flags=re.I)
    assert runtime_mismatches(runtime, moved)


def test_a_missing_hash_fails():
    dev = read(DEV)
    unhashed = re.sub(r"(?m)^(pytest==\S+) \\\n(?:\s+--hash=sha256:[0-9a-f]{64}(?: \\)?\n)+", r"\1\n", dev)
    assert unhashed != dev
    assert header_and_hash_problems(unhashed, "requirements-dev.txt", "requirements-dev.in")


# ---------------------------------------------------------------- the minimum-versions lockfile
MIN_IN = REPO / ".github" / "min-deps" / "requirements.in"
MIN = REPO / ".github" / "min-deps" / "requirements.txt"


def lower_bounds(dependencies: list[str]) -> dict[str, str]:
    """`name>=1.2` -> {name: 1.2}; a dependency without a lower bound maps to ''."""
    out = {}
    for dep in dependencies:
        m = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9_.-]*)\s*(?:>=\s*([^\s,;]+))?.*", dep)
        out[m.group(1).lower()] = m.group(2) or ""
    return out


def min_pin_problems(bounds: dict[str, str], min_text: str, dev_text: str) -> list[str]:
    """The minimum-deps job proves pyproject.toml's lower bounds only if it installs exactly them."""
    pins, dev = entries(min_text), entries(dev_text)
    problems = [f"{name}: no lower bound in pyproject.toml" for name, low in bounds.items() if not low]
    problems += [f"{name}: pyproject.toml says >={low}, the minimum lockfile has {pins.get(name, ('missing',))[0]}"
                 for name, low in bounds.items() if low and pins.get(name, ("",))[0] != low]
    problems += [f"{tool}: {pins.get(tool, ('missing',))[0]} in the minimum lockfile, {dev[tool][0]} in requirements-dev.txt"
                 for tool in ("pytest", "hypothesis") if pins.get(tool, ("",))[0] != dev[tool][0]]
    return problems


def test_the_minimum_lockfile_pins_exactly_the_lower_bounds_and_the_dev_test_tools():
    import tomllib
    deps = tomllib.loads(read(REPO / "pyproject.toml"))["project"]["dependencies"]
    assert min_pin_problems(lower_bounds(deps), read(MIN), read(DEV)) == []
    assert header_and_hash_problems(read(MIN), ".github/min-deps/requirements.txt", ".github/min-deps/requirements.in") == []
    direct = {ln.split("#")[0].split("==")[0].strip().lower() for ln in read(MIN_IN).splitlines() if ln.split("#")[0].strip()}
    assert direct == set(lower_bounds(deps)) | {"pytest", "hypothesis"}
    assert set(lower_bounds(deps)) == {ln.split("#")[0].strip().lower() for ln in read(REPO / "requirements.in").splitlines()
                                       if ln.split("#")[0].strip()}, "pyproject.toml and requirements.in name the same packages"


def test_a_raised_bound_without_a_new_minimum_lockfile_fails():
    import tomllib
    deps = tomllib.loads(read(REPO / "pyproject.toml"))["project"]["dependencies"]
    bounds = lower_bounds(deps)
    raised = {**bounds, "httpx": "99.0"}
    assert min_pin_problems(raised, read(MIN), read(DEV)) == [
        f"httpx: pyproject.toml says >=99.0, the minimum lockfile has {entries(read(MIN))['httpx'][0]}"]
    assert min_pin_problems({**bounds, "httpx": ""}, read(MIN), read(DEV)) == ["httpx: no lower bound in pyproject.toml"]
