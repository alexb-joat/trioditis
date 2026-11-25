"""A stand-in for a local Ollama server, for rehearsing `trioditis results` without a model.

    python scripts/fake_ollama.py --port 18080

Its numbers measure nothing. The answers are a fixed function of the prompt text, chosen only so that the
counts are neither all right nor all wrong; the model `tiny:1b` is a made-up name and the version string says
so. Never quote anything this server produces as a result.

What it answers, the way Ollama does (shapes as the test double `Server` in tests/test_measure.py uses):

    GET  /api/version          {"version": "0.0.0-standin"}
    GET  /api/tags             {"models": [{"name", "model", "digest"} ...]}  (digests are 64 hex characters)
    POST /v1/chat/completions  {"choices": [{"message": {"content": "<route>"}}]}
    POST /v1/embeddings        {"data": [{"index", "embedding"} ...]}

It binds 127.0.0.1 only, takes no other address, never logs a request body and keeps nothing on disk.
Standard library only, so it runs wherever Python does.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "127.0.0.1"
VERSION = "0.0.0-standin"
MODEL = "tiny:1b"                     # made up: no such model exists
EMBEDDINGS_MODEL = "nomic-embed-text"
ROUTES = ("code", "reasoning", "writing", "quick")
DIMS = 16
MAX_BODY = 1_000_000                  # a request larger than this is not one of ours


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

    def _json(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= MAX_BODY:
                return None
            body = json.loads(self.rfile.read(length))
        except (ValueError, OSError):
            return None
        return body if isinstance(body, dict) else None

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/api/version":
            self._send(200, {"version": VERSION})
        elif self.path == "/api/tags":
            names = (MODEL, f"{EMBEDDINGS_MODEL}:latest")
            self._send(200, {"models": [{"name": n, "model": n, "digest": digest(n)} for n in names]})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        body = self._json()
        if body is None:
            self._send(400, {"error": "bad request"})
        elif self.path == "/v1/chat/completions":
            messages = body.get("messages")
            text = messages[-1].get("content", "") if isinstance(messages, list) and messages and isinstance(messages[-1], dict) else ""
            self._send(200, {"choices": [{"message": {"role": "assistant", "content": chat_reply(str(text))}}]})
        elif self.path == "/v1/embeddings":
            texts = body.get("input")
            texts = [texts] if isinstance(texts, str) else texts
            if not isinstance(texts, list):
                self._send(400, {"error": "input must be a list of texts"})
            else:
                self._send(200, {"data": [{"index": i, "embedding": embed(str(t))} for i, t in enumerate(texts)]})
        else:
            self._send(404, {"error": "not found"})


def make_server(port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((HOST, port), Handler)
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="A stand-in for a local Ollama server; its numbers measure nothing.")
    ap.add_argument("--port", type=int, required=True, help="the port on 127.0.0.1 to listen on")
    args = ap.parse_args(argv)
    if not 1 <= args.port <= 65535:
        ap.error("--port must be from 1 to 65535")
    server = make_server(args.port)
    print(f"fake-ollama on http://{HOST}:{args.port} (model {MODEL}; its numbers measure nothing)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
