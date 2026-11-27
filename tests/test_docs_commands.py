"""Every command the README and docs show is either run here or listed with the reason it can't be.

RUN commands run offline, in a fresh home folder, with Ollama pointed at a closed port, and must exit 0. MANUAL
ones need a network, a model, Docker, another program or a file of your own; they are checked by hand before a
release. A new command in the docs fails this test until it is put in one of the two lists, and a listed command
that the docs no longer show fails too, so the lists can't drift from the pages.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
DOC_FILES = [REPO / "README.md", *sorted((REPO / "docs").glob("*.md"))]
FENCE = re.compile(r"```(\w*)\n(.*?)```", re.S)
NOT_SHELL = {"json", "yaml", "text", "csv", "toml"}
COMMAND = re.compile(r"^(trioditis|python3?|py|pipx|pip|git|ollama|curl|docker|cp|mkdir|cd|d=|scripts[/\\]|ANTHROPIC_)")

# Run here, in this order, from the folder `trioditis init --dir ~/trioditis` makes (scripts from the repository).
RUN = [
    "trioditis init --dir ~/trioditis",
    "trioditis check",
    'trioditis explain "Why does my python function raise KeyError?"',
    "trioditis hardware",
    "trioditis hardware --json",
    "trioditis usage",
    "trioditis usage --since 7d --by client",
    "trioditis usage --since month --by day",
    "trioditis usage --session eval/run-7 --json",
    "trioditis usage --plan --since 7d",
    "trioditis plan import --claude-code --dry-run",
    "trioditis refit --dry-run",
    "trioditis refit",
    "trioditis refit --json",
    "python -m trioditis init",  # in a second, empty folder: the clone's first command
    "python scripts/stats_walkthrough.py",
]
# One bash block in docs/route-accuracy.md (a variable and brace expansion): run as written, through bash.
RUN_BASH = [
    "d=docs/route-accuracy/picker-models",
    "python scripts/compare_results.py compare --baseline $d/results-qwen2.5-coder-7b-run1.answers.json "
    "$d/results-{llama3.2-3b,qwen2.5-3b,qwen2.5-coder-3b,gemma3-4b,phi4-mini-3.8b,granite4-3b,ministral-3-3b,"
    "smollm2-1.7b}-run1.answers.json",
    "python scripts/compare_results.py changed $d/results-granite4-3b-run1.answers.json "
    "$d/results-granite4-3b-run2.answers.json",
]
NET = "downloads from the network"
SERVER = "needs `trioditis serve` running"
MODEL = "needs a running Ollama with the model pulled"
OWN_FILE = "needs a file of your own (the format is shown next to it)"
MANUAL = {
    "ollama pull llama3.2:3b && ollama pull qwen2.5-coder:7b && ollama pull nomic-embed-text": NET,
    "pipx install trioditis": NET,
    "git clone https://github.com/alexb-joat/trioditis.git && cd trioditis": NET,
    "git clone https://github.com/alexb-joat/trioditis.git; cd trioditis": NET,
    "pip install --require-hashes --no-deps -r requirements.txt": NET + " (CI installs the same lockfile)",
    "python3 -m venv .venv && source .venv/bin/activate": "makes a virtual environment; CI's setup does the same",
    r"py -3 -m venv .venv; .\.venv\Scripts\Activate.ps1": "Windows PowerShell; CI's setup does the same",
    "cd ~/trioditis": "changes folder; the commands after it run there in this test",
    "trioditis serve": "starts the server and keeps running; scripts/smoke.py runs it in CI",
    "curl -s http://127.0.0.1:8787/v1/chat/completions -H 'Content-Type: application/json' "
    """-d '{"model":"auto","messages":[{"role":"user","content":"Write a regex for UK postcodes"}]}'""": SERVER,
    "trioditis results": MODEL + "; times the models for minutes",
    "scripts/claude-t": "starts Claude Code, which must be installed",
    r"scripts\claude-t.ps1": "starts Claude Code, which must be installed (Windows)",
    r"scripts\claude-t.cmd": "starts Claude Code, which must be installed (Windows)",
    "ANTHROPIC_BASE_URL=http://127.0.0.1:8787 claude": "starts Claude Code; " + SERVER,
    'ANTHROPIC_CUSTOM_HEADERS="X-Trioditis-Key: <your router key>" ANTHROPIC_BASE_URL=http://127.0.0.1:8787 claude':
        "starts Claude Code with your router key; " + SERVER,
    "cp config.example.yaml config.yaml": "Docker setup; scripts/docker_smoke.py does the same in CI",
    "cp .env.example .env": "Docker setup; scripts/docker_smoke.py does the same in CI",
    "mkdir -p logs": "Docker setup; scripts/docker_smoke.py does the same in CI",
    "docker compose up -d --build": "needs Docker; scripts/docker_smoke.py builds and runs the image in CI",
    "python scripts/route_study.py heldout docs/route-accuracy/labels.csv --settled docs/route-accuracy/settled.csv "
    "--out trioditis/data/routes-heldout.csv": "rewrites the packaged labels file; run by hand when the labels change",
    "trioditis eval-routes --labels trioditis/data/routes-heldout.csv --config config.example.yaml "
    "--compare-threshold 0.55": MODEL + " (embeddings)",
    "python scripts/picker_timing.py --model qwen2.5-coder:7b --lengths 100,150,200,250,300 --n 20": MODEL,
    "python scripts/route_study.py model-baseline trioditis/data/routes-heldout.csv --model qwen2.5-coder:7b "
    "--config config.example.yaml": MODEL,
    "trioditis prices import provider-facts.json --dry-run": OWN_FILE,
    "trioditis prices import provider-facts.json": OWN_FILE,
    "trioditis plan import --claude-code": "imports your real Claude Code transcripts into metrics.db",
    "trioditis plan import events.json": OWN_FILE,
    "trioditis plan import --meter meter.json": OWN_FILE,
    "python scripts/effort_trial.py run --prompts prompts.json --out ~/trial --meter-file METER_FILE":
        OWN_FILE + "; spends plan usage on real models",
    "python scripts/effort_trial.py report --out ~/trial": "reads the output of `effort_trial.py run`",
    "python scripts/effort_trial.py blind --out ~/trial --prompts prompts.json": "reads the output of `effort_trial.py run`",
    "trioditis eval-routes --labels labels.csv": OWN_FILE + "; with embeddings on, " + MODEL,
    "trioditis eval-routes --labels new-labels.csv --compare-threshold 0.45": OWN_FILE + "; " + MODEL,
    "trioditis eval-routes --log": "needs a decision log from real traffic",
}


def doc_commands() -> dict[str, str]:
    """Command (continuations joined, trailing comment dropped) -> the first file that shows it."""
    found: dict[str, str] = {}
    for path in DOC_FILES:
        for lang, body in FENCE.findall(path.read_text(encoding="utf-8")):
            if lang in NOT_SHELL:
                continue
            for line in re.sub(r"\s*\\\n\s*", " ", body).splitlines():
                line = re.sub(r"\s+#\s.*$", "", line).strip()
                if COMMAND.match(line):
                    found.setdefault(line, path.relative_to(REPO).as_posix())
    return found


def test_every_command_in_the_docs_is_run_here_or_listed_with_a_reason():
    shown = doc_commands()
    assert len(shown) > 40, "the parser must see the docs' commands"
    listed = set(RUN) | set(RUN_BASH) | set(MANUAL)
    unlisted = sorted(f"{file}: {cmd}" for cmd, file in shown.items() if cmd not in listed)
    assert unlisted == [], "add each to RUN (offline, exits 0) or MANUAL (with the reason):\n" + "\n".join(unlisted)
    gone = sorted(listed - set(shown))
    assert gone == [], "listed here but no longer in the docs:\n" + "\n".join(gone)
    assert set(RUN).isdisjoint(MANUAL) and set(RUN_BASH).isdisjoint(MANUAL)


def _env(home: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if k != "HLIDSKJALV_HOME" and not k.startswith(("TRIODITIS_", "ANTHROPIC_", "OPENAI_", "OPENROUTER_", "CLAUDE_"))}
    env.update(HOME=str(home), USERPROFILE=str(home), PYTHONPATH=str(REPO), PYTHONIOENCODING="utf-8",
               PATH=os.pathsep.join([str(Path(sys.executable).parent), env.get("PATH", "")]))
    return env


def _argv(cmd: str, home: Path) -> list[str]:
    argv = [a.replace("~/", f"{home.as_posix()}/") for a in shlex.split(cmd)]
    if argv[0] == "trioditis":
        return [sys.executable, "-m", "trioditis", *argv[1:]]
    assert argv[0] == "python", cmd
    return [sys.executable, *argv[1:]]


def test_the_offline_commands_run_and_exit_0(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env = _env(home)
    folder = home / "trioditis"
    failed = []
    for cmd in RUN:
        if cmd == "python -m trioditis init":
            cwd = tmp_path / "clone"
            cwd.mkdir()
        elif cmd.startswith("python scripts/"):
            cwd = REPO
        else:
            cwd = folder if folder.exists() else tmp_path
        out = subprocess.run(_argv(cmd, home), cwd=cwd, env=env, capture_output=True, text=True, timeout=120,
                             encoding="utf-8", errors="replace")
        if out.returncode != 0:
            failed.append(f"{cmd}: exit {out.returncode}\n{(out.stderr or out.stdout)[-600:]}")
        if cmd.startswith("trioditis init"):
            # Ollama on a closed port: the test never depends on (or loads models in) a local Ollama.
            config = folder / "config.yaml"
            config.write_text(re.sub(r"http://(localhost|127\.0\.0\.1):11434", "http://127.0.0.1:9",
                                     config.read_text(encoding="utf-8")), encoding="utf-8")
    assert failed == [], "\n\n".join(failed)


@pytest.mark.skipif(shutil.which("bash") is None or sys.platform == "win32", reason="the block is bash")
def test_the_bash_block_runs_as_written(tmp_path):
    out = subprocess.run(["bash", "-euc", "\n".join(RUN_BASH)], cwd=REPO, env=_env(tmp_path), capture_output=True,
                         text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-1000:]
