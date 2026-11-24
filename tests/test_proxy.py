"""Proxy variables (HTTP_PROXY and friends) must not carry local calls off the machine.

A prompt held back by the privacy check goes to a local model. If the router's client then
obeyed HTTP_PROXY for that call, the whole prompt would be handed to the proxy. Cloud calls
keep using the proxy: on a network that needs one they cannot leave any other way.

Real sockets, loopback only: a mocked transport would switch proxy handling off and prove nothing.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from trioditis.app import create_app
from trioditis.config import parse_config
from trioditis_metrics import BudgetCaps, BudgetGuard, MetricsDB, Prices

PROXY_VARS = ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy")


class _Proxy(BaseHTTPRequestHandler):
    """Stands in for a proxy on another machine: records what reaches it and answers 502."""

    def _record(self):
        self.server.hits.append(f"{self.command} {self.path}")
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(502)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = do_POST = do_CONNECT = _record

    def log_message(self, *a):
        pass


class _LocalModels(BaseHTTPRequestHandler):
    """Stands in for Ollama's OpenAI-style server."""

    def do_POST(self):
        sent = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        self.server.hits.append(f"POST {self.path}")
        if self.path.endswith("/embeddings"):
            out = {"data": [{"index": i, "embedding": [1.0, 0.0]} for i, _ in enumerate(sent["input"])]}
        else:
            out = {"id": "c1", "model": sent["model"],
                   "choices": [{"index": 0, "message": {"role": "assistant", "content": "code"}, "finish_reason": "stop"}],
                   "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
        raw = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self.server.hits.append(f"GET {self.path}")
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture
def servers(monkeypatch):
    made = []
    for handler in (_Proxy, _LocalModels):
        s = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        s.hits = []
        threading.Thread(target=s.serve_forever, daemon=True).start()
        made.append(s)
    proxy, local = made
    for name in PROXY_VARS:
        monkeypatch.setenv(name, f"http://127.0.0.1:{proxy.server_port}")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.setenv("CLOUD_KEY", "cloud-secret")
    monkeypatch.delenv("TRIODITIS_API_KEY", raising=False)
    yield proxy, local
    for s in made:
        s.shutdown()
        s.server_close()


def _cfg(local_port: int, **over):
    raw = {
        "providers": {
            "local": {"base_url": f"http://127.0.0.1:{local_port}/v1", "local": True},
            "cloud": {"base_url": "https://api.example.com/v1", "api_key_env": "CLOUD_KEY"},
        },
        "models": {
            "small": {"provider": "local", "id": "llama3.2:3b"},
            "big": {"provider": "cloud", "id": "big-1"},
        },
        "routes": {
            "code": {"models": ["big"], "keywords": ["python"], "examples": ["fix this function"],
                     "description": "writing or fixing code"},
            "quick": {"models": ["small"], "examples": ["what time is it"], "description": "short questions"},
        },
        "default_route": "quick",
        "logging": {"path": None},
    }
    raw.update(over)
    return parse_config(raw)


def _app(cfg, tmp_path):
    # No transport argument: httpx switches proxy handling off when it is given one.
    metrics = MetricsDB(tmp_path / "metrics.db")
    return create_app(cfg, metrics=metrics, budget=BudgetGuard(metrics, BudgetCaps()), prices=Prices(tmp_path / "prices.json"),
                      hardware_file=tmp_path / "hardware.json", accounts_file=tmp_path / "accounts.json")


def _client(app):
    # The test client itself must not use the stand-in proxy, and needs a Host the router answers to.
    return TestClient(app, base_url="http://127.0.0.1:8787")


def _ask(app, text, model="auto"):
    return _client(app).post("/v1/chat/completions",
                                json={"model": model, "messages": [{"role": "user", "content": text}]})


def test_a_local_model_call_does_not_go_to_the_proxy(servers, tmp_path):
    proxy, local = servers
    app = _app(_cfg(local.server_port), tmp_path)
    r = _ask(app, "my password is hunter2, say hello")  # the privacy check keeps this one local
    assert r.status_code == 200, r.text
    assert local.hits == ["POST /v1/chat/completions"]
    assert proxy.hits == []


def test_local_embeddings_and_the_route_picker_do_not_go_to_the_proxy(servers, tmp_path):
    proxy, local = servers
    cfg = _cfg(local.server_port, embeddings={"provider": "local", "model": "e"},
               route_picker={"provider": "local", "model": "p", "timeout_ms": 5000})
    app = _app(cfg, tmp_path)
    r = _ask(app, "hello there", model="route/quick")  # warm path: one local chat call
    assert r.status_code == 200, r.text
    r = _client(app).post("/v1/route", json={"model": "auto", "messages": [{"role": "user", "content": "hello there"}]})
    assert r.status_code == 200, r.text
    assert "POST /v1/embeddings" in local.hits
    assert local.hits.count("POST /v1/chat/completions") == 2  # the answer, then the picker
    assert proxy.hits == []


def test_a_cloud_call_still_uses_the_proxy(servers, tmp_path):
    proxy, local = servers
    app = _app(_cfg(local.server_port), tmp_path)
    _ask(app, "hello", model="big")  # the stand-in proxy answers 502, so the call itself fails
    assert proxy.hits == ["CONNECT api.example.com:443"]
