"""The Anthropic Messages passthrough (trioditis/gate.py): what goes up, what comes back, what is kept."""
from __future__ import annotations

import asyncio
import gzip
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.test_trioditis import cfg
from trioditis.app import create_app
from trioditis_metrics import BudgetCaps, BudgetGuard, MetricsDB, Prices

TOKEN = "sk-ant-oat01-" + "T0KEN" * 12          # a login token's shape; must never land in any file
ROUTER_KEY = "r" * 40
MSG_ID = "msg_01ABCdefGHIjkl"
CLAUDE_HEADERS = {
    "authorization": f"Bearer {TOKEN}",
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "oauth-2025-04-20,interleaved-thinking-2025-05-14",
    "anthropic-future-thing": "kept as sent",       # an anthropic-* header nobody has seen yet
    "x-claude-code-session-id": "5f0c6c1e-session",
    "user-agent": "claude-cli/2.1.300 (external, cli)",
    "content-type": "application/json",
}
BODY = b'{"model": "claude-haiku-4-5",  "max_tokens": 16, "stream": true,\n "messages": [{"role": "user", "content": "hi"}]}'

SSE = [
    b'event: message_start\ndata: {"type":"message_start","message":{"id":"' + MSG_ID.encode() + b'","type":"message",'
    b'"role":"assistant","model":"claude-haiku-4-5-20251001","content":[],"usage":{"input_tokens":12,'
    b'"cache_creation_input_tokens":300,"cache_read_input_tokens":4000,"output_tokens":1}}}\n\n',
    b'event: ping\ndata: {"type": "ping"}\n\n',
    b'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
    b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"ok"}}\n\n',
    b'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n'
    b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":7}}\n\n',
    b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
]


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks, gate: asyncio.Event | None = None):
        self.chunks, self.gate = chunks, gate

    async def __aiter__(self):
        for i, c in enumerate(self.chunks):
            if i == 1 and self.gate is not None:
                await asyncio.wait_for(self.gate.wait(), 5)  # the rest only once the client has the first chunk
            yield c


def wire(status: int, content: bytes, **headers) -> httpx.Response:
    """A response as the network delivers it: an unread stream (httpx pre-reads one built with content=)."""
    return httpx.Response(status, headers=headers, stream=Chunks([content]))


def sse_response(gate=None, **headers):
    return httpx.Response(200, headers={"content-type": "text/event-stream", "request-id": "req_1",
                                        "anthropic-ratelimit-unified-5h-utilization": "0.12", **headers},
                          stream=Chunks(SSE, gate))


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HLIDSKJALV_HOME", str(tmp_path / "hl"))
    monkeypatch.delenv("TRIODITIS_GATE_UPSTREAM", raising=False)
    return tmp_path


def make_app(home: Path, handler, **over):
    metrics = MetricsDB(home / "metrics.db")
    app = create_app(cfg(logging={"path": str(home / "logs" / "decisions.jsonl")}, **over),
                     transport=httpx.MockTransport(handler), metrics=metrics,
                     budget=BudgetGuard(metrics, BudgetCaps()), prices=Prices(home / "prices.json"),
                     hardware_file=home / "hardware.json", accounts_file=home / "accounts.json")
    return app, metrics


def client_for(app) -> TestClient:
    return TestClient(app, base_url="http://127.0.0.1:8787")


def test_the_request_reaches_anthropic_as_it_was_sent(home):
    seen = {}

    def upstream(request: httpx.Request):
        seen.update(url=str(request.url), body=request.content, headers=dict(request.headers))
        return sse_response()
    app, _ = make_app(home, upstream)
    r = client_for(app).post("/v1/messages?beta=true", content=BODY,
                             headers={**CLAUDE_HEADERS, "x-trioditis-client": "mine"})
    assert r.status_code == 200
    assert seen["url"] == "https://api.anthropic.com/v1/messages?beta=true" and seen["body"] == BODY
    h = seen["headers"]
    for k, v in CLAUDE_HEADERS.items():
        assert h[k] == v, k  # Authorization, every anthropic-* and x-claude-code-* header, byte for byte
    assert h["host"] == "api.anthropic.com" and h["accept-encoding"] == "identity"
    assert not any(k.startswith("x-trioditis-") for k in h)  # our own headers stay here


def test_the_stream_comes_back_byte_for_byte_with_the_headers_claude_code_reads(home):
    app, _ = make_app(home, lambda req: sse_response(**{"retry-after": "3", "x-should-retry": "false"}))
    r = client_for(app).post("/v1/messages?beta=true", content=BODY, headers=CLAUDE_HEADERS)
    assert r.content == b"".join(SSE)  # ping events included: Claude Code's idle watchdog counts on them
    assert r.headers["content-type"] == "text/event-stream" and r.headers["request-id"] == "req_1"
    assert r.headers["anthropic-ratelimit-unified-5h-utilization"] == "0.12"
    assert r.headers["retry-after"] == "3" and r.headers["x-should-retry"] == "false"


def test_the_stream_is_not_buffered(home):
    """The upstream holds its second chunk until the client has the first: a gate that buffered would wait for
    the end and the upstream would time out. Driven through the ASGI interface, so nothing in between buffers."""
    async def go():
        gate = asyncio.Event()
        app, _ = make_app(home, lambda req: sse_response(gate))
        bodies: list[bytes] = []
        sent = False

        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": BODY, "more_body": False}
            await asyncio.sleep(3600)

        async def send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                bodies.append(message["body"])
                if len(bodies) == 1:
                    gate.set()
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
                 "scheme": "http", "path": "/v1/messages", "raw_path": b"/v1/messages", "query_string": b"beta=true",
                 "root_path": "", "server": ("127.0.0.1", 8787), "client": ("127.0.0.1", 50000),
                 "headers": [(k.encode(), v.encode()) for k, v in {**CLAUDE_HEADERS, "host": "127.0.0.1:8787"}.items()]}
        await asyncio.wait_for(app(scope, receive, send), 10)
        return bodies
    bodies = asyncio.run(go())
    assert bodies[0] == SSE[0] and b"".join(bodies) == b"".join(SSE)


def test_an_error_passes_through_untouched(home):
    """Claude Code recovers from rejected capabilities by the upstream's exact wording, so error bodies are
    never wrapped, and the status and headers stay as they were."""
    err = b'{"type":"error","error":{"type":"authentication_error","message":"OAuth token has expired."}}'
    app, metrics = make_app(home, lambda req: wire(
        401, err, **{"content-type": "application/json", "request-id": "req_2", "x-should-retry": "false"}))
    r = client_for(app).post("/v1/messages?beta=true", content=BODY, headers=CLAUDE_HEADERS)
    assert (r.status_code, r.content, r.headers["request-id"], r.headers["x-should-retry"]) == \
        (401, err, "req_2", "false")
    assert metrics.conn.execute("SELECT COUNT(*) FROM plan_usage").fetchone()[0] == 0


def test_plan_usage_is_metered_and_merges_with_the_transcript_import(home):
    from trioditis.plan import load_salt, transcript_events
    from trioditis_metrics.plan import merge_events
    app, metrics = make_app(home, lambda req: sse_response())
    client_for(app).post("/v1/messages?beta=true", content=BODY,
                         headers={**CLAUDE_HEADERS, "x-claude-code-agent-id": "agent-7"})
    rows = [dict(r) for r in metrics.conn.execute("SELECT * FROM plan_usage")]
    assert len(rows) == 1
    row = rows[0]
    assert (row["source"], row["model"], row["tokens_in"], row["cache_read"], row["cache_write"], row["tokens_out"],
            row["agent"], row["tokens_measured"]) == ("claude-code", "claude-haiku-4-5-20251001", 12, 4000, 300, 7, 1, 1)
    assert metrics.conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 0  # the plan's use is not spend
    # The same message, later imported from Claude Code's transcript, is the same row.
    transcript = home / "t.jsonl"
    transcript.write_text(json.dumps({"type": "assistant", "timestamp": "2026-11-18T18:00:00Z", "isSidechain": True,
                                      "sessionId": "5f0c6c1e-session", "message": {
                                          "id": MSG_ID, "model": "claude-haiku-4-5-20251001",
                                          "usage": {"input_tokens": 12, "output_tokens": 7}}}) + "\n")
    events = transcript_events(transcript, load_salt(create=False))
    assert events[0]["id"] == row["id"] and events[0]["thread_fp"] == row["thread_fp"]
    with metrics.conn:
        assert merge_events(metrics.conn, events)["added"] == 0


def test_api_key_calls_are_spend_not_plan_usage(home):
    (home / "prices.json").write_text(json.dumps({"claude-haiku-4-5-20251001": {"input": 1.0, "output": 5.0}}))
    app, metrics = make_app(home, lambda req: sse_response())
    headers = {k: v for k, v in CLAUDE_HEADERS.items() if k != "authorization"} | {"x-api-key": "sk-ant-api03-" + "K" * 40}
    client_for(app).post("/v1/messages", content=BODY, headers=headers)
    [row] = [dict(r) for r in metrics.conn.execute("SELECT * FROM calls")]
    assert (row["route"], row["provider"], row["tokens_in"], row["tokens_out"], row["cached_tokens"], row["local"]) == \
        ("passthrough", "anthropic", 4312, 7, 4000, 0)
    assert row["cost"] == pytest.approx((4312 * 1.0 + 7 * 5.0) / 1e6)
    assert metrics.conn.execute("SELECT COUNT(*) FROM plan_usage").fetchone()[0] == 0


def test_the_token_never_lands_in_a_file(home):
    """Logs, metrics and the salt, after a good call, an error, a refusal and an unreachable upstream."""
    answers = iter([sse_response(), wire(529, b'{"type":"error"}')])

    def upstream(req):
        try:
            return next(answers)
        except StopIteration:
            raise httpx.ConnectError("down", request=req) from None
    app, metrics = make_app(home, upstream)
    c = client_for(app)
    c.post("/v1/messages?beta=true", content=BODY, headers=CLAUDE_HEADERS)
    c.post("/v1/messages?beta=true", content=BODY, headers=CLAUDE_HEADERS)
    assert c.post("/v1/messages", content=BODY, headers=CLAUDE_HEADERS).status_code == 502
    c.post("/v1/messages", content=BODY, headers={**CLAUDE_HEADERS, "host": "evil.example"})  # refused, logged
    metrics.conn.commit()
    files = [p for p in home.rglob("*") if p.is_file()]
    assert any(p.name == "security.jsonl" for p in files)
    for p in files:
        assert TOKEN.encode() not in p.read_bytes() and b"T0KEN" not in p.read_bytes(), p


def test_an_unreachable_upstream_says_how_to_detach(home):
    def down(req):
        raise httpx.ConnectError("[Errno 101] Network is unreachable", request=req)
    app, _ = make_app(home, down)
    r = client_for(app).post("/v1/messages", content=BODY, headers=CLAUDE_HEADERS)
    assert r.status_code == 502 and r.json()["type"] == "error"
    assert r.json()["error"]["message"] == ("trioditis could not reach https://api.anthropic.com: ConnectError. "
                                            "Detach with `unset ANTHROPIC_BASE_URL` to go direct.")


def test_a_body_over_the_limit_is_refused_before_anthropic_sees_it(home):
    calls = []
    app, _ = make_app(home, lambda req: calls.append(req) or sse_response(), limits={"max_body_bytes": 100})
    chunks = iter([BODY[:60], BODY[60:] + b" " * 100])  # streamed, so no Content-Length to check first
    r = client_for(app).post("/v1/messages", content=chunks, headers=CLAUDE_HEADERS)
    assert r.status_code == 413 and r.headers["x-should-retry"] == "false"
    assert r.json() == {"type": "error", "error": {"type": "request_too_large",
                                                   "message": "request body over 100 bytes (limits.max_body_bytes)"}}
    assert calls == []


def test_count_tokens_passes_through_and_is_not_metered(home):
    seen = []
    app, metrics = make_app(home, lambda req: seen.append(req.url.path) or wire(
        200, b'{"input_tokens": 42}', **{"content-type": "application/json"}))
    r = client_for(app).post("/v1/messages/count_tokens?beta=true", content=b'{"model":"m","messages":[]}',
                             headers=CLAUDE_HEADERS)
    assert r.json() == {"input_tokens": 42} and seen == ["/v1/messages/count_tokens"]
    assert metrics.conn.execute("SELECT COUNT(*) FROM plan_usage").fetchone()[0] == 0


def test_with_a_router_key_the_gate_takes_it_in_its_own_header(home, monkeypatch):
    monkeypatch.setenv("TRIODITIS_API_KEY", ROUTER_KEY)
    seen = []
    app, _ = make_app(home, lambda req: seen.append(dict(req.headers)) or sse_response())
    c = client_for(app)
    r = c.post("/v1/messages", content=BODY, headers=CLAUDE_HEADERS)
    assert r.status_code == 401 and seen == [] and r.headers["x-should-retry"] == "false"
    r = c.post("/v1/messages", content=BODY, headers={**CLAUDE_HEADERS, "x-trioditis-key": ROUTER_KEY})
    assert r.status_code == 200 and seen[0]["authorization"] == f"Bearer {TOKEN}" and "x-trioditis-key" not in seen[0]
    # The router key sent as Authorization would travel to Anthropic: refused before anything goes out.
    r = c.post("/v1/messages", content=BODY, headers={**CLAUDE_HEADERS, "authorization": f"Bearer {ROUTER_KEY}",
                                                      "x-trioditis-key": ROUTER_KEY})
    assert r.status_code == 401 and len(seen) == 1 and "X-Trioditis-Key" in r.json()["error"]["message"]


@pytest.mark.parametrize("carrier", [
    {"authorization": f"bearer {ROUTER_KEY}"},
    {"authorization": f"Bearer  {ROUTER_KEY} "},
    {"authorization": ROUTER_KEY},
    {"x-api-key": ROUTER_KEY},
    {"x-api-key": f" {ROUTER_KEY}"},
])
def test_the_router_key_never_travels_in_a_credential_header(home, monkeypatch, carrier):
    monkeypatch.setenv("TRIODITIS_API_KEY", ROUTER_KEY)
    seen = []
    app, _ = make_app(home, lambda req: seen.append(1) or sse_response())
    r = client_for(app).post("/v1/messages", content=BODY,
                             headers={**CLAUDE_HEADERS, "x-trioditis-key": ROUTER_KEY, **carrier})
    assert r.status_code == 401 and seen == []
    assert f"it arrived in {next(iter(carrier))}" in r.json()["error"]["message"]
    assert ROUTER_KEY not in r.text


def test_the_router_key_is_caught_in_a_second_header_and_with_a_space_in_it(home, monkeypatch):
    monkeypatch.setenv("TRIODITIS_API_KEY", ROUTER_KEY)
    seen = []
    app, _ = make_app(home, lambda req: seen.append(1) or sse_response())
    twice = [(k, v) for k, v in {**CLAUDE_HEADERS, "x-trioditis-key": ROUTER_KEY}.items()]
    twice += [("x-api-key", "sk-ant-other"), ("x-api-key", ROUTER_KEY)]
    r = client_for(app).post("/v1/messages", content=BODY, headers=twice)
    assert r.status_code == 401 and seen == []
    spaced = "a router key with spaces, long enough to bind"
    monkeypatch.setenv("TRIODITIS_API_KEY", spaced)
    app, _ = make_app(home, lambda req: seen.append(1) or sse_response())
    r = client_for(app).post("/v1/messages", content=BODY,
                             headers={**CLAUDE_HEADERS, "x-trioditis-key": spaced, "authorization": f"Bearer {spaced}"})
    assert r.status_code == 401 and seen == []


def test_a_compressed_answer_keeps_its_content_encoding(home):
    # accept-encoding: identity is a request, not a guarantee. The bytes are relayed as they came,
    # so the header that says how to read them has to come along.
    packed = gzip.compress(b'{"type":"error","error":{"type":"overloaded_error","message":"x"}}')
    app, _ = make_app(home, lambda req: wire(529, packed, **{"content-type": "application/json",
                                                             "content-encoding": "gzip"}))
    r = client_for(app).post("/v1/messages", content=BODY, headers=CLAUDE_HEADERS)
    assert r.status_code == 529 and r.headers["content-encoding"] == "gzip"
    assert r.json()["error"]["type"] == "overloaded_error"


def test_the_upstream_must_be_https(home, monkeypatch):
    monkeypatch.setenv("TRIODITIS_GATE_UPSTREAM", "http://api.anthropic.com")
    with pytest.raises(ValueError, match="https://"):
        make_app(home, lambda req: sse_response())


def test_the_upstream_carries_no_user_or_password(home, monkeypatch):
    monkeypatch.setenv("TRIODITIS_GATE_UPSTREAM", "https://api.anthropic.com:secret@other.example")
    with pytest.raises(ValueError, match="no user or password") as e:
        make_app(home, lambda req: sse_response())
    assert "secret" not in str(e.value)


def test_health_says_the_gate_is_there(home):
    """claude-t reads this before routing: a router older than the gate answers /health but not /v1/messages."""
    app, _ = make_app(home, lambda req: sse_response())
    assert client_for(app).get("/health").json()["anthropic_gate"] is True
