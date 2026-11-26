"""scripts/claude-t (sh), claude-t.ps1 and claude-t.cmd, run for real against a stand-in router and a
stand-in `claude`. Each launcher runs where its shell exists: sh on POSIX, ps1 wherever PowerShell is
installed (Windows PowerShell 5.1 or PowerShell 7), cmd on Windows."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
from fake_ollama import LocalServer  # noqa: E402  (no reverse-DNS lookup at bind: it stalls on macOS runners)

WINDOWS = os.name == "nt"
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")


def _launchers():
    sh = pytest.param(lambda: ["sh", str(SCRIPTS / "claude-t")], id="sh", marks=pytest.mark.skipif(
        WINDOWS or shutil.which("curl") is None, reason="claude-t is a /bin/sh script that calls curl"))
    ps1 = pytest.param(lambda: [POWERSHELL, "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
                                "-File", str(SCRIPTS / "claude-t.ps1")], id="ps1",
                       marks=pytest.mark.skipif(POWERSHELL is None, reason="no PowerShell on this machine"))
    cmd = pytest.param(lambda: ["cmd.exe", "/d", "/c", str(SCRIPTS / "claude-t.cmd")], id="cmd",
                       marks=pytest.mark.skipif(not WINDOWS, reason="claude-t.cmd is for Windows cmd"))
    return [sh, ps1, cmd]


@pytest.fixture(params=_launchers())
def launcher(request):
    return request.param()


@pytest.fixture
def router():
    class Health(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = b'{"ok":true,"anthropic_gate":true}'  # compact, as the gate's /health sends it
            self.send_response(200)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = LocalServer(("127.0.0.1", 0), Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def fake_claude(bin_dir: Path, seen: Path) -> None:
    """A `claude` on PATH that writes the environment and arguments it was started with."""
    dump = bin_dir / "dump.py"
    dump.write_text("import json, os, sys\n"
                    f"json.dump({{'env': dict(os.environ), 'argv': sys.argv[1:]}}, open({str(seen)!r}, 'w'))\n")
    if WINDOWS:
        (bin_dir / "claude.cmd").write_text(f'@"{sys.executable}" "{dump}" %*\r\n')
    else:
        fake = bin_dir / "claude"
        fake.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{dump}" "$@"\n')
        fake.chmod(0o755)


def run(launcher: list[str], tmp_path: Path, url: str, *args: str, **env: str) -> subprocess.CompletedProcess:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake_claude(bin_dir, tmp_path / "seen.json")
    base = {k: v for k, v in os.environ.items()
            if k not in ("ENABLE_TOOL_SEARCH", "ANTHROPIC_BASE_URL", "ANTHROPIC_CUSTOM_HEADERS", "TRIODITIS_API_KEY")}
    base.update(PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}", TRIODITIS_URL=url, **env)
    return subprocess.run([*launcher, *args], env=base, timeout=60, capture_output=True, text=True)


def seen(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "seen.json").read_text())


def test_through_the_gate_the_base_url_is_set_and_tool_search_stays_on(launcher, tmp_path, router):
    # Claude Code 2.1.296 sent all 289 tools in full through a non-Anthropic base URL unless this was set.
    p = run(launcher, tmp_path, router, "-p", "say hi")
    assert p.returncode == 0, p.stderr
    s = seen(tmp_path)
    assert s["env"]["ANTHROPIC_BASE_URL"] == router
    assert s["env"]["ENABLE_TOOL_SEARCH"] == "true"
    assert s["argv"] == ["-p", "say hi"]


def test_a_tool_search_value_you_set_is_kept(launcher, tmp_path, router):
    run(launcher, tmp_path, router, ENABLE_TOOL_SEARCH="auto:5")
    assert seen(tmp_path)["env"]["ENABLE_TOOL_SEARCH"] == "auto:5"


def test_the_router_key_goes_in_its_own_header(launcher, tmp_path, router):
    run(launcher, tmp_path, router, TRIODITIS_API_KEY="r" * 40, ANTHROPIC_CUSTOM_HEADERS="X-Other: 1")
    headers = seen(tmp_path)["env"]["ANTHROPIC_CUSTOM_HEADERS"]
    assert headers.splitlines() == ["X-Other: 1", "X-Trioditis-Key: " + "r" * 40]


def test_with_no_router_it_starts_direct_and_says_so(launcher, tmp_path):
    p = run(launcher, tmp_path, "http://127.0.0.1:9")     # nothing listens on the discard port
    assert p.returncode == 0, p.stderr
    env = seen(tmp_path)["env"]
    assert "ANTHROPIC_BASE_URL" not in env and "ENABLE_TOOL_SEARCH" not in env
    assert "starting Claude Code direct, not metered" in p.stderr


@pytest.mark.parametrize("url, says", [
    ("http://user:pw@127.0.0.1:8787", "has an @ in it"),
    ("http://192.168.1.5:8787", "Plain http is only for this machine"),
])
def test_an_unsafe_url_is_refused_before_anything_starts(launcher, tmp_path, url, says):
    p = run(launcher, tmp_path, url)
    assert p.returncode == 2 and says in p.stderr
    assert not (tmp_path / "seen.json").exists()
