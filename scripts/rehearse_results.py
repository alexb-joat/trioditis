"""Run `trioditis results` for real, end to end, against the stand-in server, and check the file it writes.

    python scripts/rehearse_results.py --python PATH --source-tree    # the code in this checkout
    python scripts/rehearse_results.py --python PATH --installed      # a wheel installed in PATH's environment

It starts scripts/fake_ollama.py on a free port (127.0.0.1 only) as a child it stops by its own handle, runs
`PATH -m trioditis results --no-share --model tiny:1b --base-url ... --out ... --answers ...` over real sockets,
then checks the file with `scripts/collect_results.py check`, the way a shared submission is checked, and the
answers file for the shape the comparison reads. The child runs in an
isolated environment: its own HLIDSKJALV_HOME and TRIODITIS_ENV_FILE inside a temporary folder, and proxy
variables pointing at a dead address to show that the run ignores them. Your environment is not passed on.

The stand-in's numbers measure nothing; this checks that the program runs and its file passes the collector,
never how good a model is. It calls no model and sends nothing off this machine.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FAKE = ROOT / "scripts" / "fake_ollama.py"
COLLECTOR = ROOT / "scripts" / "collect_results.py"
MODEL = "tiny:1b"                          # the stand-in's made-up model name
DEAD_PROXY = "http://127.0.0.1:9"          # nothing listens there: a run that honoured it would fail
KEEP = ("PATH", "SYSTEMROOT", "SYSTEMDRIVE", "PATHEXT", "COMSPEC", "WINDIR", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE")
NEVER = {8787, 8788, 11434}                # the live router's ports and Ollama's own: never used here
TIMEOUT_S = 300


class RehearsalError(Exception):
    """The rehearsal failed; the message says where and shows the tail of what the child printed."""


def free_port() -> int:
    while True:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if port not in NEVER:
            return port


def child_env(home: Path) -> dict[str, str]:
    """A small environment built from nothing: the run's own variables, not the caller's."""
    env = {k: os.environ[k] for k in KEEP if k in os.environ}
    env.update({"HLIDSKJALV_HOME": str(home / "hlidskjalv"), "TRIODITIS_ENV_FILE": str(home / "no-such.env"),
                "HOME": str(home), "USERPROFILE": str(home), "APPDATA": str(home), "LOCALAPPDATA": str(home),
                "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"})
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        env[name] = DEAD_PROXY
    return env


def run(cmd: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=TIMEOUT_S, check=False)


def tail(text: str, n: int = 1500) -> str:
    text = text.strip()
    return text if len(text) <= n else "..." + text[-n:]


def wait_until_up(url: str, proc: subprocess.Popen, log: Path) -> None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))     # never through a proxy
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RehearsalError(f"the stand-in server exited with code {proc.returncode}: {tail(log.read_text(errors='replace'))}")
        try:
            with opener.open(url, timeout=1) as r:
                if r.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            time.sleep(0.1)
    raise RehearsalError(f"the stand-in server did not answer {url} within 15 s")


def check_answers(answers: dict, doc: dict) -> None:
    """The `--answers` file has the shape the comparison reads and refers to the same held-out set as the results."""
    if set(answers) != {"schema", "trioditis", "heldout_sha256_16", "picker", "answers"}:
        raise RehearsalError(f"the answers file has unexpected keys: {sorted(answers)}")
    if answers["heldout_sha256_16"] != doc["settings"]["heldout_sha256_16"] or answers["picker"] != doc["models"]["picker"]:
        raise RehearsalError("the answers file and the results file name a different held-out set or picker")
    rows = answers["answers"]
    n = doc["heldout"]["n"]
    if [r.get("row") for r in rows] != list(range(2, n + 2)) or any(set(r) != {"row", "route"} for r in rows):
        raise RehearsalError(f"the answers file does not hold one {{row, route}} entry for each of the {n} rows")
    if not {r["route"] for r in rows} <= {*doc["settings"]["routes"], "-"}:
        raise RehearsalError("the answers file names a route that is not one of the shipped routes")


def rehearse(python: str, mode: str, repo: Path = ROOT) -> str:
    """The one-line result when the run passed; RehearsalError otherwise. `mode` is "source tree" or "installed wheel"."""
    if mode not in ("source tree", "installed wheel"):
        raise ValueError(mode)
    with tempfile.TemporaryDirectory(prefix="trioditis-rehearsal-") as tmp:
        home = Path(tmp)
        work = repo if mode == "source tree" else home / "work"
        work.mkdir(exist_ok=True)
        out = home / "trioditis-results.json"
        answers_out = home / "trioditis-answers.json"
        env = child_env(home)

        where = run([python, "-c", "import json, trioditis; print(json.dumps({'file': trioditis.__file__, "
                                   "'version': trioditis.__version__}))"], cwd=work, env=env)
        if where.returncode != 0:
            raise RehearsalError(f"{python} cannot import trioditis from {work}: {tail(where.stderr)}")
        info = json.loads(where.stdout.strip().splitlines()[-1])
        inside = Path(info["file"]).resolve().is_relative_to(repo.resolve())
        if inside != (mode == "source tree"):
            raise RehearsalError(f"{mode}: trioditis was imported from {info['file']}, "
                                 f"{'expected the checkout' if mode == 'source tree' else 'expected the installed wheel, not the checkout'}")

        port = free_port()
        log = home / "fake-ollama.log"
        with log.open("w", encoding="utf-8") as logfile:
            server = subprocess.Popen([sys.executable, str(FAKE), "--port", str(port)], stdout=logfile,
                                      stderr=subprocess.STDOUT, cwd=home)
            try:
                wait_until_up(f"http://127.0.0.1:{port}/api/version", server, log)
                r = run([python, "-m", "trioditis", "results", "--no-share", "--model", MODEL,
                         "--base-url", f"http://127.0.0.1:{port}/v1", "--out", str(out),
                         "--answers", str(answers_out)], cwd=work, env=env)
            finally:
                server.terminate()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()
        if r.returncode != 0:
            raise RehearsalError(f"`trioditis results` exited {r.returncode}: {tail(r.stderr or r.stdout)}")
        if not out.is_file():
            raise RehearsalError(f"`trioditis results` exited 0 but wrote no file at {out}")

        doc = json.loads(out.read_text(encoding="utf-8"))
        if not answers_out.is_file():
            raise RehearsalError(f"`trioditis results --answers` exited 0 but wrote no file at {answers_out}")
        check_answers(json.loads(answers_out.read_text(encoding="utf-8")), doc)
        c = run([python, str(COLLECTOR), "check", str(out)], cwd=work, env=env)
        if c.returncode != 0 or ": ok," not in c.stdout:
            raise RehearsalError(f"the collector did not accept the file: {tail(c.stdout + c.stderr)}")
        return f"results rehearsal OK: trioditis {info['version']} ({mode}), {doc['heldout']['n']} prompts, collector accepted"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--python", required=True, metavar="PATH", help="the interpreter to run `trioditis` with")
    where = ap.add_mutually_exclusive_group(required=True)
    where.add_argument("--source-tree", action="store_true", help="run the code in this checkout (the working folder is its root)")
    where.add_argument("--installed", action="store_true", help="run the installed wheel from a folder outside the checkout")
    args = ap.parse_args(argv)
    try:
        print(rehearse(args.python, "source tree" if args.source_tree else "installed wheel"))
    except (RehearsalError, subprocess.TimeoutExpired, OSError) as err:
        print(f"results rehearsal FAILED: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
