"""scripts/claude-t, run for real against a stand-in router and a stand-in `claude` (POSIX shells only)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "claude-t"

pytestmark = pytest.mark.skipif(
    os.name != "posix" or shutil.which("curl") is None, reason="claude-t is a /bin/sh script that calls curl")


@pytest.fixture
def router():
    class Health(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = b'{"ok":true,"anthropic_gate":true}'
            self.send_response(200)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = HTTPServer(("127.0.0.1", 0), Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def run_claude_t(tmp_path: Path, url: str, **env: str) -> dict:
    """Run claude-t with a fake `claude` that writes the environment it was started with."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    seen = tmp_path / "env.json"
    fake = bin_dir / "claude"
    fake.write_text(f'#!/bin/sh\nexec python3 -c "import json,os; json.dump(dict(os.environ), open(\'{seen}\', \'w\'))"\n')
    fake.chmod(0o755)
    base = {k: v for k, v in os.environ.items() if k not in ("ENABLE_TOOL_SEARCH", "ANTHROPIC_BASE_URL")}
    base.update(PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}", TRIODITIS_URL=url, **env)
    subprocess.run(["sh", str(SCRIPT)], env=base, check=True, timeout=30, capture_output=True)
    return json.loads(seen.read_text())


def test_tool_search_stays_on_through_the_gate(tmp_path, router):
    # Claude Code 2.1.296 sent all 289 tools in full through a non-Anthropic base URL unless this was set.
    env = run_claude_t(tmp_path, router)
    assert env["ANTHROPIC_BASE_URL"] == router
    assert env["ENABLE_TOOL_SEARCH"] == "true"


def test_a_tool_search_value_you_set_is_kept(tmp_path, router):
    env = run_claude_t(tmp_path, router, ENABLE_TOOL_SEARCH="auto:5")
    assert env["ENABLE_TOOL_SEARCH"] == "auto:5"


def test_direct_start_changes_nothing(tmp_path):
    env = run_claude_t(tmp_path, "http://127.0.0.1:9")     # nothing listens on the discard port
    assert "ANTHROPIC_BASE_URL" not in env
    assert "ENABLE_TOOL_SEARCH" not in env
