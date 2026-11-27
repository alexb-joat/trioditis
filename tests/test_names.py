"""Hlidskjalv, the dashboard's old name, survives in Trioditis only as the old data folder and the
old variable that the upgrade reads. The dashboard itself is Lunete now."""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
# Allowed: the old folder (~/.hlidskjalv, /app/logs/hlidskjalv) and the old variable.
OLD_PATH_OR_VARIABLE = re.compile(r"[./]hlidskjalv|HLIDSKJALV_HOME")
# In the docs, a path to a file inside the old folder is never allowed: files live in ~/.trioditis.
OLD_FILE_PATH = re.compile(r"\.hlidskjalv/\w")
EXEMPT = {
    "CHANGELOG.md",           # past entries record what things were called then
    "tests/test_names.py",    # this file
    "tests/test_migrate.py",  # builds the old layout on purpose
}
DOCS = re.compile(r"(README\.md|docs/.*\.md|config\.example\.yaml)$")


def _tracked_lines():
    if shutil.which("git") is None or not (ROOT / ".git").exists():
        pytest.skip("needs a git checkout")
    out = subprocess.run(["git", "grep", "-n", "-I", "-i", "hlidskjalv"], cwd=ROOT,
                         capture_output=True, text=True, check=False)
    assert out.returncode in (0, 1), out.stderr
    for line in out.stdout.splitlines():
        path, number, text = line.split(":", 2)
        if path not in EXEMPT:
            yield path, number, text


def test_the_old_name_appears_only_as_the_old_folder_or_variable():
    stray = []
    for path, number, text in _tracked_lines():
        leftover = OLD_PATH_OR_VARIABLE.sub("", text)
        if re.search("hlidskjalv", leftover, re.IGNORECASE):
            stray.append(f"{path}:{number}: {text.strip()[:120]}")
    assert stray == [], "say Lunete for the dashboard, or name the old folder or variable:\n" + "\n".join(stray)


def test_the_docs_point_at_files_in_the_new_folder():
    stray = [f"{path}:{number}: {text.strip()[:120]}" for path, number, text in _tracked_lines()
             if DOCS.search(path) and OLD_FILE_PATH.search(text)]
    assert stray == [], "write ~/.trioditis/<file>, not the old folder:\n" + "\n".join(stray)
