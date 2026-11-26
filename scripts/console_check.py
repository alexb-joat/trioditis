"""Claude Code through the gate, started from each console the machine has (CI runs it on Linux, macOS
and Windows; .github/workflows/consoles.yml).

For each console (bash, zsh, PowerShell 7, Windows PowerShell 5.1, cmd, Git Bash) and each Claude Code
build given with --claude, it starts `claude -p hi` twice: through the console's claude-t launcher, and
with ANTHROPIC_BASE_URL set by hand the way docs/claude-code.md shows for that console. Requests go
through the real router to a local https stand-in for Anthropic, which records what arrived and answers
400, so no model is called and nothing is billed. Per run it checks:

  - tool search: whether Claude Code deferred tools (`defer_loading`) or sent every one in full;
  - the shadow count: one new line in logs/shadow.jsonl, with the same number of tools.

Expected: tool search on through every launcher; by hand, on except for builds named with --old, which
turn it off for any base URL that isn't Anthropic's (2.1.296 did). Writes only inside --dir (a temp
folder by default); Claude Code gets its own CLAUDE_CONFIG_DIR there and a made-up API key.

    python scripts/console_check.py --claude 2.1.296=<dir with claude> --claude 2.1.330=<dir> --old 2.1.296
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class LocalServer(ThreadingHTTPServer):
    """ThreadingHTTPServer without the reverse-DNS lookup of its own address (socket.getfqdn in
    HTTPServer.server_bind), which can stall for many seconds on GitHub's macOS runners. Only CGI reads server_name."""

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
WINDOWS = os.name == "nt"
PROMPT = "hi"

CONFIG = """
providers:
  local: {base_url: "http://127.0.0.1:9/v1", local: true}
models:
  small: {provider: local, id: tiny}
  coder: {provider: local, id: coder}
routes:
  code: {models: [coder], keywords: [python], description: Writing or debugging code}
  quick: {models: [small], description: Short questions}
default_route: quick
route_picker: {provider: local, model: tiny, timeout_ms: 500, shadow_claude_code: true}
logging: {path: "%s"}
"""

OPENSSL_CNF = """[req]
distinguished_name = dn
x509_extensions = ext
prompt = no
[dn]
CN = 127.0.0.1
[ext]
subjectAltName = IP:127.0.0.1
basicConstraints = critical,CA:TRUE
keyUsage = critical,digitalSignature,keyCertSign
extendedKeyUsage = serverAuth
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid
"""


# ------------------------------------------------------------------ the stand-in for Anthropic

class Capture:
    def __init__(self):
        self.lock = threading.Lock()
        self.records: list[dict] = []

    def since(self, n: int) -> list[dict]:
        with self.lock:
            return self.records[n:]

    def __len__(self):
        with self.lock:
            return len(self.records)


def stub(capture: Capture, cert: Path, key: Path) -> int:
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("content-length") or 0))
            try:
                body = json.loads(raw)
            except ValueError:
                body = {}
            tools = body.get("tools") or [] if isinstance(body, dict) else []
            with capture.lock:
                capture.records.append({"path": self.path, "bytes": len(raw), "tools": len(tools),
                                        "deferred": sum(1 for t in tools if isinstance(t, dict) and t.get("defer_loading"))})
            out = json.dumps({"type": "error", "error": {"type": "invalid_request_error",
                                                         "message": "console_check stand-in: nothing is answered"}}).encode()
            self.send_response(400)
            self.send_header("content-type", "application/json")
            self.send_header("x-should-retry", "false")
            self.send_header("content-length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

    server = LocalServer(("127.0.0.1", 0), H)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2  # PROTOCOL_TLS_SERVER alone still accepts TLS 1.0 and 1.1
    ctx.load_cert_chain(str(cert), str(key))
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server.server_address[1]


def openssl() -> str:
    found = shutil.which("openssl")
    for p in ([] if found else [r"C:\Program Files\Git\usr\bin\openssl.exe", r"C:\Program Files\OpenSSL\bin\openssl.exe"]):
        if Path(p).exists():
            found = p
    if not found:
        sys.exit("console_check: no openssl on PATH (on Windows, Git for Windows ships one): needed for the stand-in's certificate")
    return found


def certificate(work: Path) -> tuple[Path, Path]:
    cnf, cert, key = work / "openssl.cnf", work / "cert.pem", work / "key.pem"
    cnf.write_text(OPENSSL_CNF)
    subprocess.run([openssl(), "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-config", str(cnf),
                    "-keyout", str(key), "-out", str(cert)], check=True, capture_output=True)
    return cert, key


# ------------------------------------------------------------------ the router

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_router(work: Path, stub_port: int, cert: Path) -> tuple[subprocess.Popen, str]:
    cfg = work / "config.yaml"
    cfg.write_text(CONFIG % (work / "logs" / "decisions.jsonl").as_posix())
    port = free_port()
    env = {**os.environ, "TRIODITIS_GATE_UPSTREAM": f"https://127.0.0.1:{stub_port}", "SSL_CERT_FILE": str(cert),
           "HLIDSKJALV_HOME": str(work / "home"),
           "PYTHONPATH": os.pathsep.join(filter(None, [str(ROOT), os.environ.get("PYTHONPATH")]))}
    env.pop("TRIODITIS_API_KEY", None)  # and cwd=work below: the repo's .env (a router key) is not read
    log = open(work / "router.log", "w")
    proc = subprocess.Popen([sys.executable, "-m", "trioditis", "--config", str(cfg), "serve",
                             "--host", "127.0.0.1", "--port", str(port)], cwd=work, env=env, stdout=log, stderr=log)
    url = f"http://127.0.0.1:{port}"
    for _ in range(120):
        try:
            with urllib.request.urlopen(f"{url}/health", timeout=1) as r:
                if json.loads(r.read()).get("anthropic_gate") is True:
                    return proc, url
        except OSError:
            pass
        if proc.poll() is not None:
            break
        time.sleep(0.25)
    proc.kill()
    sys.exit(f"console_check: the router did not come up; its output is in {work / 'router.log'}")


# ------------------------------------------------------------------ the consoles

def consoles() -> dict[str, tuple[str, str]]:
    """name -> (executable, how it is found). Only consoles this machine has."""
    found = {}
    want = (["powershell", "pwsh", "cmd", "gitbash"] if WINDOWS else ["bash", "zsh", "pwsh"])
    for name in want:
        exe = (r"C:\Program Files\Git\bin\bash.exe" if name == "gitbash"
               else shutil.which("cmd.exe") if name == "cmd" else shutil.which(name))
        if exe and Path(exe).exists():
            found[name] = exe
    return found


def command(console: str, exe: str, how: str, gate: str) -> list[str] | str:
    """The command line a person would type in that console, as one non-interactive run."""
    sh_t = (SCRIPTS / "claude-t").as_posix()
    if console in ("bash", "zsh", "gitbash"):
        line = f'"{sh_t}" -p {PROMPT}' if how == "launcher" else f'ANTHROPIC_BASE_URL={gate} claude -p {PROMPT}'
        return [exe, "-c", line]
    if console in ("pwsh", "powershell"):
        line = (f"& '{SCRIPTS / 'claude-t.ps1'}' -p {PROMPT}" if how == "launcher"
                else f"$env:ANTHROPIC_BASE_URL = '{gate}'; claude -p {PROMPT}")
        policy = ["-ExecutionPolicy", "Bypass"] if console == "powershell" else []
        return [exe, "-NoLogo", "-NoProfile", *policy, "-Command", line]
    # cmd parses its own command line: hand it a string, not a list Python would quote its way
    line = (f'"{SCRIPTS / "claude-t.cmd"}" -p {PROMPT}' if how == "launcher"
            else f'set "ANTHROPIC_BASE_URL={gate}" && claude -p {PROMPT}')
    return f'"{exe}" /d /s /c "{line}"'


def shadow_lines(work: Path) -> list[dict]:
    p = work / "logs" / "shadow.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.exists() else []


def run_case(console, exe, how, version, bin_dir, gate, work, capture) -> dict:
    before, shadows = len(capture), len(shadow_lines(work))
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("ANTHROPIC_", "CLAUDE_")) and k not in ("ENABLE_TOOL_SEARCH", "TRIODITIS_API_KEY")}
    env.update(PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}", TRIODITIS_URL=gate,
               ANTHROPIC_API_KEY="sk-ant-api03-" + "console-check-not-a-key-" * 3,
               CLAUDE_CONFIG_DIR=str(work / f"claude-{version}"))
    cwd = work / "project"
    cwd.mkdir(exist_ok=True)
    t0 = time.monotonic()
    try:
        p = subprocess.run(command(console, exe, how, gate), env=env, cwd=cwd, capture_output=True, text=True,
                           timeout=180, errors="replace")
        tail = (p.stderr or p.stdout).strip().splitlines()[-1:] or [""]
    except subprocess.TimeoutExpired:
        tail = ["no exit in 180 s"]
    time.sleep(1.0)  # the shadow line is written in the background after the request
    sent = [r for r in capture.since(before) if r["path"].startswith("/v1/messages") and "count_tokens" not in r["path"]
            and r["tools"]]
    new_shadow = shadow_lines(work)[shadows:]
    first = sent[0] if sent else None
    return {"console": console, "how": how, "claude": version, "s": round(time.monotonic() - t0, 1),
            "mode": None if first is None else ("search" if first["deferred"] else "inline"),
            "tools": first and first["tools"], "deferred": first and first["deferred"], "kb": first and first["bytes"] // 1024,
            "shadow": len(new_shadow), "shadow_tools": new_shadow[0].get("tools") if new_shadow else None,
            "last_line": tail[0][:160]}


# ------------------------------------------------------------------ main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--claude", action="append", required=True, metavar="VERSION=DIR",
                    help="a Claude Code build: the folder that has its `claude` command (repeatable)")
    ap.add_argument("--old", action="append", default=[], metavar="VERSION",
                    help="a build that turns tool search off for non-Anthropic base URLs (expected: inline by hand)")
    ap.add_argument("--require", default="", help="comma-separated consoles that must exist here, e.g. zsh,bash,pwsh")
    ap.add_argument("--dir", help="work folder (default: a new temp folder)")
    ap.add_argument("--json", help="also write the results here")
    args = ap.parse_args()

    builds = dict(c.split("=", 1) for c in args.claude)
    work = Path(args.dir or tempfile.mkdtemp(prefix="console-check-")).resolve()
    work.mkdir(parents=True, exist_ok=True)
    have = consoles()
    missing = [c for c in filter(None, args.require.split(",")) if c not in have]

    capture = Capture()
    cert, key = certificate(work)
    router, gate = start_router(work, stub(capture, cert, key), cert)
    results = []
    try:
        for console, exe in have.items():
            for version, bin_dir in builds.items():
                for how in ("launcher", "by hand"):
                    r = run_case(console, exe, how, version, bin_dir, gate, work, capture)
                    r["expect"] = "inline" if how == "by hand" and version in args.old else "search"
                    r["ok"] = r["mode"] == r["expect"] and r["shadow"] == 1 and r["shadow_tools"] == r["tools"]
                    results.append(r)
                    print(f"{'ok ' if r['ok'] else 'BAD'} {console:<10} {how:<8} claude {version:<8} "
                          f"tools {r['tools']} deferred {r['deferred']} ({r['kb']} KB) -> {r['mode']} "
                          f"(expected {r['expect']}); shadow lines {r['shadow']} tools {r['shadow_tools']}; {r['s']} s",
                          flush=True)
                    if not r["ok"]:
                        print(f"    last line: {r['last_line']}", flush=True)
    finally:
        router.terminate()
        try:
            router.wait(10)
        except subprocess.TimeoutExpired:
            router.kill()

    bad = [r for r in results if not r["ok"]]
    summary = [f"### Claude Code through the gate, {sys.platform}", "",
               "| console | how | claude | tools | deferred | KB | tool search | expected | shadow |",
               "|---|---|---|---|---|---|---|---|---|"]
    summary += [f"| {r['console']} | {r['how']} | {r['claude']} | {r['tools']} | {r['deferred']} | {r['kb']} | "
                f"{r['mode']} | {r['expect']} | {r['shadow']} |" for r in results]
    if missing:
        summary += ["", f"Required but not found here: {', '.join(missing)}"]
    summary += ["", f"{len(results) - len(bad)} of {len(results)} runs as expected."]
    print("\n".join(summary[-1:]))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write("\n".join(summary) + "\n")
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=1))
    if missing:
        print(f"console_check: required consoles not on this machine: {', '.join(missing)}", file=sys.stderr)
    return 1 if bad or missing or not results else 0


if __name__ == "__main__":
    sys.exit(main())
