"""A stand-in for a local Ollama server, for rehearsing `trioditis results` without a model.

    python scripts/fake_ollama.py --port 18080

Its numbers measure nothing. The answers are a fixed function of the prompt text, chosen only so that the
counts are neither all right nor all wrong; the model `tiny:1b` is a made-up name and the version string says
so. Never quote anything this server produces as a result.

What it answers, the way Ollama does (shapes as the test double `Server` in tests/test_measure.py uses):

    GET  /api/version          {"version": "0.0.0-standin"}
    GET  /api/tags             {"models": [{"name", "model", "digest"} ...]}  (digests are 64 hex characters)
    GET  /api/ps               {"models": [{"name", "model", "size", "digest", "expires_at", "size_vram"} ...]}
    POST /v1/chat/completions  {"choices": [{"message": {"content": "<route>"}}]}
    POST /v1/embeddings        {"data": [{"index", "embedding"} ...]}
    POST /api/generate         an empty request only ({"model", "keep_alive"?}, no prompt): loads the model and
                               keeps it for keep_alive ("30m", "45s", "1h" or seconds; a negative value keeps
                               it, 0 drops it at once), as Ollama's FAQ describes; {"model", "response": "", "done": true}

It binds 127.0.0.1 only, takes no other address, never logs a request body and keeps nothing on disk.
Standard library only, so it runs wherever Python does.

`--mode` makes it misbehave the way a stranger's server might, to see what `trioditis results` tells them:

    chat-500          the chat endpoint answers HTTP 500
    chat-not-json     the chat endpoint answers 200 with a body that is not JSON
    chat-wrong-shape  the chat endpoint answers 200 with JSON that is not a chat completion
    no-embeddings     the embeddings endpoint answers 404
    no-api            /api/version, /api/tags and /api/ps answer 404, as servers other than Ollama do
    close-after       after --after N requests it closes every connection without answering

Loading, the way Ollama keeps a model in memory, so the route picker's cold starts, timeouts and busy turns
can be reproduced (tests/test_picker_speed.py). A chat or embeddings request loads the model it names; /api/ps
lists a model only once its load has finished, and drops it --keep-alive seconds after its last request ended.

    --load-delay S    a chat whose model is not loaded (the first after start, or after the keep-alive ran
                      out) answers only after S seconds; a chat that arrives during the load waits for it
                      too (default 0: loads are instant)
    --keep-alive S    seconds a model stays loaded after its last request ends (default 300, Ollama's "5m")
    --per-char S      a chat also waits S seconds per character of its last message (default 0)

These delays are settings for a test, not a model of any machine: they measure nothing either.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import socketserver
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "127.0.0.1"
VERSION = "0.0.0-standin"
MODEL = "tiny:1b"                     # made up: no such model exists
EMBEDDINGS_MODEL = "nomic-embed-text"
ROUTES = ("code", "reasoning", "writing", "quick")
DIMS = 16
MAX_BODY = 1_000_000                  # a request larger than this is not one of ours
MODES = ("ok", "chat-500", "chat-not-json", "chat-wrong-shape", "no-embeddings", "no-api", "close-after")
KEEP_ALIVE_S = 300.0                  # Ollama's default keep-alive, "5m"
MODEL_SIZE = 1_000_000_000            # the bytes /api/ps reports for every model: made up
MAX_MODELS = 16                       # model names remembered, oldest dropped first


def digest(name: str) -> str:
    """A made-up but well-formed digest: 64 lowercase hex characters, as Ollama's /api/tags lists."""
    return hashlib.sha256(f"stand-in:{name}".encode()).hexdigest()


def chat_reply(text: str) -> str:
    """One route name, a fixed function of the text: about a quarter of the answers match any label."""
    return ROUTES[int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:4], "big") % len(ROUTES)]


def embed(text: str) -> list[float]:
    """A hashed bag of words: texts that share words point the same way, so similarities vary."""
    vec = [0.0] * DIMS
    for word in re.findall(r"\w+", text.lower()):
        vec[int.from_bytes(hashlib.sha256(word.encode("utf-8")).digest()[:2], "big") % DIMS] += 1.0
    return vec


def keep_alive_seconds(value) -> float | None:
    """Ollama's keep_alive: a number of seconds or a duration such as "30m"; negative keeps the model
    loaded, so it becomes infinity. None when the value is not one of those."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        seconds = float(value)
    else:
        m = re.fullmatch(r"(-?\d+(?:\.\d+)?)(ms|s|m|h)?", str(value).strip())
        if not m:
            return None
        seconds = float(m.group(1)) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600, None: 1}[m.group(2)]
    return float("inf") if seconds < 0 else seconds


def _loaded(m: dict, now: float) -> bool:
    """Finished loading, and a request is running or the keep-alive has not run out."""
    return m["ready"] <= now and (m["running"] > 0 or now < m["expires"])


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "fake-ollama"
    disable_nagle_algorithm = True        # headers and body are two writes; without this each request waits ~40 ms

    def log_message(self, format: str, *args) -> None:  # noqa: A002  (the stdlib's signature) never log what was asked
        pass

    def _send(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _drops(self) -> bool:
        """Counts the request; True when it is to be dropped unanswered (mode close-after)."""
        server = self.server
        with server.lock:
            server.request_count += 1
            n = server.request_count
        if server.mode == "close-after" and n > server.after:
            self.close_connection = True
            return True
        return False

    def _json(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= MAX_BODY:
                return None
            body = json.loads(self.rfile.read(length))
        except (ValueError, OSError):
            return None
        return body if isinstance(body, dict) else None

    def _begin(self, name: str, delay: float) -> dict:
        """Start a request on model `name`: when it is neither loaded nor loading, start its load, as Ollama
        does; wait for the load to finish; return the model's state. The caller ends the request with _end."""
        server = self.server
        with server.lock:
            now = time.monotonic()
            m = server.models.get(name)
            if m is None or not (now < m["ready"] or _loaded(m, now)):
                m = {"ready": now + delay, "expires": now, "running": 0}
                server.models.pop(name, None)
                server.models[name] = m
                server.loads += 1
                while len(server.models) > MAX_MODELS:
                    server.models.pop(next(iter(server.models)))
            m["running"] += 1
        # Until the clock /api/ps reads says the load is over: on a coarse clock (15.6 ms ticks, Windows before
        # Python 3.13) one sleep can end a tick short, and the answer would come while /api/ps still lists nothing.
        while (wait := m["ready"] - time.monotonic()) > 0:
            time.sleep(wait)
        return m

    def _end(self, m: dict, keep_alive: float | None = None) -> None:
        with self.server.lock:
            m["running"] -= 1
            m["expires"] = time.monotonic() + (self.server.keep_alive if keep_alive is None else keep_alive)

    def _ps(self) -> dict:
        now, wall = time.monotonic(), datetime.now(UTC)
        with self.server.lock:
            listed = [(n, m) for n, m in self.server.models.items() if _loaded(m, now)]
            return {"models": [{"name": n, "model": n, "size": MODEL_SIZE, "digest": digest(n),
                                "expires_at": (wall + timedelta(seconds=max(m["expires"] - now, 0.0))).isoformat(),
                                "size_vram": 0} for n, m in listed]}

    def do_GET(self) -> None:  # noqa: N802
        if self._drops():
            return
        if self.path in ("/api/version", "/api/tags", "/api/ps") and self.server.mode == "no-api":
            self._send(404, {"error": "not found"})
        elif self.path == "/api/version":
            self._send(200, {"version": VERSION})
        elif self.path == "/api/tags":
            names = (MODEL, f"{EMBEDDINGS_MODEL}:latest")
            self._send(200, {"models": [{"name": n, "model": n, "digest": digest(n)} for n in names]})
        elif self.path == "/api/ps":
            self._send(200, self._ps())
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self._drops():
            return
        body = self._json()
        mode = self.server.mode
        if body is None:
            self._send(400, {"error": "bad request"})
        elif self.path == "/v1/chat/completions" and mode == "chat-500":
            self._send(500, {"error": "the model failed to load"})
        elif self.path == "/v1/chat/completions" and mode == "chat-not-json":
            data = b"<html><body>Not an API</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif self.path == "/v1/chat/completions" and mode == "chat-wrong-shape":
            self._send(200, {"object": "list", "data": []})
        elif self.path == "/v1/embeddings" and mode == "no-embeddings":
            self._send(404, {"error": "model not found"})
        elif self.path == "/api/generate" and mode == "no-api":
            self._send(404, {"error": "not found"})
        elif self.path == "/api/generate":
            keep = keep_alive_seconds(body["keep_alive"]) if "keep_alive" in body else self.server.keep_alive
            name = str(body.get("model") or "")[:100]
            if body.get("prompt") or not name:
                self._send(400, {"error": "the stand-in answers only an empty generate request with a model"})
            elif keep is None:
                self._send(400, {"error": "keep_alive must be a number of seconds or a duration such as 30m"})
            else:
                m = self._begin(name, self.server.load_delay)
                self._end(m, keep)
                with self.server.lock:
                    self.server.keep_alive_requests.append(body.get("keep_alive"))
                self._send(200, {"model": name, "response": "", "done": True})
        elif self.path == "/v1/chat/completions":
            messages = body.get("messages")
            text = messages[-1].get("content", "") if isinstance(messages, list) and messages and isinstance(messages[-1], dict) else ""
            m = self._begin(str(body.get("model") or MODEL)[:100], self.server.load_delay)
            try:
                if self.server.per_char:
                    time.sleep(self.server.per_char * len(str(text)))
                self._send(200, {"choices": [{"message": {"role": "assistant", "content": chat_reply(str(text))}}]})
            finally:
                self._end(m)
        elif self.path == "/v1/embeddings":
            texts = body.get("input")
            texts = [texts] if isinstance(texts, str) else texts
            if not isinstance(texts, list):
                self._send(400, {"error": "input must be a list of texts"})
            else:
                m = self._begin(str(body.get("model") or EMBEDDINGS_MODEL)[:100], 0.0)
                try:
                    self._send(200, {"data": [{"index": i, "embedding": embed(str(t))} for i, t in enumerate(texts)]})
                finally:
                    self._end(m)
        else:
            self._send(404, {"error": "not found"})


class LocalServer(ThreadingHTTPServer):
    """ThreadingHTTPServer without the reverse-DNS lookup of its own address. HTTPServer.server_bind calls
    socket.getfqdn(host), which can stall for many seconds on GitHub's macOS runners, longer than a caller
    waits for the server to answer. Only CGI handlers read server_name, so the bind address does."""

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


def make_server(port: int, mode: str = "ok", after: int = 0, *, load_delay: float = 0.0,
                keep_alive: float = KEEP_ALIVE_S, per_char: float = 0.0) -> ThreadingHTTPServer:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    if min(load_delay, keep_alive, per_char) < 0:
        raise ValueError("load_delay, keep_alive and per_char must be 0 or more")
    server = LocalServer((HOST, port), Handler)
    server.daemon_threads = True
    server.mode, server.after = mode, after
    server.load_delay, server.keep_alive, server.per_char = load_delay, keep_alive, per_char
    server.request_count, server.lock = 0, threading.Lock()      # tests read request_count and loads
    server.models, server.loads = {}, 0                           # in memory only, like everything else here
    server.keep_alive_requests = []                               # the keep_alive value of each empty generate
    return server


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="A stand-in for a local Ollama server; its numbers measure nothing.")
    ap.add_argument("--port", type=int, required=True, help="the port on 127.0.0.1 to listen on")
    ap.add_argument("--mode", choices=MODES, default="ok", help="how it misbehaves (default: not at all)")
    ap.add_argument("--after", type=int, default=0, metavar="N", help="with --mode close-after: answer N requests first")
    ap.add_argument("--load-delay", type=float, default=0.0, metavar="S",
                    help="seconds a chat waits when its model is not loaded (default 0)")
    ap.add_argument("--keep-alive", type=float, default=KEEP_ALIVE_S, metavar="S",
                    help=f"seconds a model stays loaded after its last request (default {KEEP_ALIVE_S:g}, Ollama's 5m)")
    ap.add_argument("--per-char", type=float, default=0.0, metavar="S",
                    help="seconds a chat also waits per character of its last message (default 0)")
    args = ap.parse_args(argv)
    if not 1 <= args.port <= 65535:
        ap.error("--port must be from 1 to 65535")
    if args.after < 0:
        ap.error("--after must be 0 or more")
    for flag in ("load_delay", "keep_alive", "per_char"):
        if getattr(args, flag) < 0:
            ap.error(f"--{flag.replace('_', '-')} must be 0 or more")
    server = make_server(args.port, args.mode, args.after, load_delay=args.load_delay, keep_alive=args.keep_alive,
                         per_char=args.per_char)
    print(f"fake-ollama on http://{HOST}:{args.port} (model {MODEL}, mode {args.mode}; its numbers measure nothing)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
