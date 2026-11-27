"""Docker smoke test: build the image and run it the way docker-compose.yml does.

Read-only filesystem, every capability dropped, no new privileges, the memory and process limits. Then it checks
what the README and docs/deployment.md promise about the container:

- `python -m trioditis --version` in the image prints the version in trioditis/__init__.py;
- without TRIODITIS_API_KEY the server refuses to start on 0.0.0.0;
- as the image's own user (10001) with a logs/ folder the host user owns, the start stops with a message that says
  what to fix, not a traceback;
- run as the host user (TRIODITIS_UID/TRIODITIS_GID in docker-compose.yml), /health answers, /v1/models needs
  the key, and the process runs as that user and can't write outside /app/logs;
- a volume from an image before 0.9.1 (files in logs/hlidskjalv/) is moved into logs/trioditis/ on the first start,
  with a relative link at each old path that also resolves from the host.

Calls no model. Needs Docker on Linux (CI runs it there). Run by hand: python scripts/docker_smoke.py
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IMAGE = "trioditis:smoke"
NAME = "trioditis-smoke"
PORT = 18787
# As in docker-compose.yml; /tmp is a tmpfs inside the container, not a host path.
HARDENING = ["--read-only", "--tmpfs", "/tmp",  # noqa: S108
             "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--memory", "512m", "--pids-limit", "128"]


def docker(*args: str, check: bool = True, timeout: float = 600) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check, timeout=timeout)


def get(path: str, key: str | None = None) -> tuple[int, str]:
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}")
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def check(ok: bool, what: str, detail: str = "") -> None:
    print(("ok    " if ok else "FAIL  ") + what + (f": {detail}" if detail and not ok else ""))
    if not ok:
        raise SystemExit(1)


def main() -> int:
    want = re.search(r'__version__ = "([^"]+)"', (ROOT / "trioditis" / "__init__.py").read_text(encoding="utf-8"))[1]
    print(f"building {IMAGE} ...", flush=True)
    docker("build", "-q", "-t", IMAGE, str(ROOT))

    version = docker("run", "--rm", *HARDENING, IMAGE, "python", "-m", "trioditis", "--version").stdout.strip()
    check(version == f"trioditis {want}", f"the image prints trioditis {want}", version)

    work = Path(tempfile.mkdtemp(prefix="trioditis-docker-"))
    try:
        shutil.copy(ROOT / "config.example.yaml", work / "config.yaml")
        old = work / "logs/hlidskjalv"
        old.mkdir(parents=True)
        (old / "budget.json").write_text(json.dumps({"daily_usd": 1.0}), encoding="utf-8")
        # As docs/deployment.md says for Linux: the container runs as the host user, so it can write logs/.
        as_host = ["--user", f"{os.getuid()}:{os.getgid()}"]
        mounts = ["-v", f"{work / 'config.yaml'}:/config/config.yaml:ro", "-v", f"{work / 'logs'}:/app/logs"]

        refused = docker("run", "--rm", *as_host, *HARDENING, *mounts, IMAGE, check=False, timeout=60)
        check(refused.returncode != 0, "without TRIODITIS_API_KEY it refuses to listen on 0.0.0.0",
              f"exit {refused.returncode}")

        key = secrets.token_hex(16)
        denied = docker("run", "--rm", "-e", f"TRIODITIS_API_KEY={key}", *HARDENING, *mounts, IMAGE,
                        check=False, timeout=60)
        check(denied.returncode != 0 and "can't write logs/decisions.jsonl" in denied.stderr
              and "Traceback" not in denied.stderr, "as uid 10001 on a host-owned logs/, it says what to fix",
              denied.stderr[-1500:])

        docker("run", "-d", "--name", NAME, "-p", f"127.0.0.1:{PORT}:8787", "-e", f"TRIODITIS_API_KEY={key}",
               *as_host, *HARDENING, *mounts, IMAGE)
        status, body = 0, ""
        for _ in range(60):
            try:
                status, body = get("/health")
                break
            except OSError:  # not listening yet (URLError is an OSError)
                time.sleep(0.5)
        check(status == 200, "/health answers", f"{status} {body[:200]}\n{docker('logs', NAME, check=False).stderr[-2000:]}")
        check(get("/v1/models")[0] == 401, "/v1/models without the key is 401")
        status, body = get("/v1/models", key)
        check(status == 200 and '"auto"' in body, "/v1/models with the key lists auto", f"{status} {body[:200]}")
        check(docker("exec", NAME, "id", "-u").stdout.strip() == str(os.getuid()), "the server runs as the host user")
        check(docker("exec", NAME, "touch", "/app/x", check=False).returncode != 0, "/app is read-only")

        moved, link = work / "logs" / "trioditis" / "budget.json", old / "budget.json"
        check(moved.is_file() and not moved.is_symlink(), "the pre-0.9.1 file moved into logs/trioditis/")
        check(link.is_symlink() and not os.path.isabs(os.readlink(link)) and link.resolve() == moved.resolve(),
              "a relative link at the old path resolves from the host", str(os.readlink(link)) if link.is_symlink() else "")
    finally:
        docker("rm", "-f", NAME, check=False)
        shutil.rmtree(work, ignore_errors=True)
    print("docker smoke test passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
