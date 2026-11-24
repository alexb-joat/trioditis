"""Anthropic Messages passthrough: Claude Code, or any Anthropic-format client, through Trioditis.

Point a client at the router with ``ANTHROPIC_BASE_URL=http://127.0.0.1:8787``. Each request goes to
Anthropic as it was sent: the body bytes, the ``anthropic-*`` headers as an open list, and the client's
own credential (``Authorization`` or ``x-api-key``). The answer comes back as it was sent: status, headers,
error bodies, and the event stream chunk by chunk, ``ping`` events included. Claude Code's gateway guide
asks for all of that: it stalls on a buffered stream and recovers from errors by their exact wording.

What Trioditis adds is metering, from the response and never from the prompt: token counts, model,
message id. The credential is passed on and never stored or logged; two things are checked: whether an
``x-api-key`` is present, which says the call is billed per token rather than on a plan, and that
neither credential header carries the router key.
Model choice stays with the client: a base URL changes where requests go, not which model answers.
"""

from __future__ import annotations

import hmac
import json
import time
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from trioditis_metrics import MetricsDB, Prices
from trioditis_metrics.plan import merge_events

from .envvars import env
from .plan import _count, _hash, _word, load_salt
from .security import fingerprint

UPSTREAM = "https://api.anthropic.com"
MESSAGES = "/v1/messages"
COUNT_TOKENS = "/v1/messages/count_tokens"
ROUTER_KEY_HEADER = "x-trioditis-key"
PATHS = frozenset({MESSAGES, COUNT_TOKENS})

# Request headers that belong to this hop, not to the request: everything else goes upstream as sent.
# accept-encoding is replaced by "identity" so the stream arrives as plain events and leaves as it came.
# content-encoding is passed down: the body is relayed as raw bytes, so if the upstream compresses
# anyway, the client still learns how to read it (the call is then not metered).
_HOP_REQUEST = frozenset({"host", "connection", "keep-alive", "proxy-authorization", "proxy-connection", "te",
                          "trailer", "transfer-encoding", "upgrade", "content-length", "accept-encoding"})
_HOP_RESPONSE = frozenset({"connection", "keep-alive", "transfer-encoding", "content-length",
                           "trailer", "upgrade", "proxy-authenticate"})
# The two headers that carry a credential to Anthropic: the router key must not arrive in either.
_CREDENTIAL_HEADERS = ("authorization", "x-api-key")

# Read timeout between two bytes: a long thinking pause still gets the upstream's ping events.
TIMEOUT = httpx.Timeout(connect=10.0, read=600.0, write=60.0, pool=10.0)
METER_LINE_MAX = 1 << 20      # one event line kept for metering; a longer one passes through unparsed
METER_JSON_MAX = 4 << 20      # a non-streamed answer parsed for its usage up to this size


def _carries(value: str, key: str) -> bool:
    """The header value is the key, has it as one of its words, or is a scheme word followed by it
    (the last form catches a key with a space in it)."""
    whole = value.strip()
    after_scheme = whole.split(None, 1)[1:]  # what follows the first word, if anything
    return any(hmac.compare_digest(part.encode(), key.encode()) for part in [whole, *whole.split(), *after_scheme])


def upstream_base() -> str:
    url = (env("GATE_UPSTREAM") or UPSTREAM).rstrip("/")
    try:
        parts = urlsplit(url)
        ok = parts.scheme == "https" and bool(parts.hostname) and parts.username is None and parts.password is None
    except ValueError:
        ok = False
    if not ok:
        raise ValueError("TRIODITIS_GATE_UPSTREAM must be an https:// URL with no user or password in it "
                         "(the credential travels with every request), got "
                         f"{url.split('@')[-1][:80]!r}")
    return url


def key_ok(headers, expected: str) -> bool:
    """With a router key set, a gate request carries it in X-Trioditis-Key: Authorization is Anthropic's.
    Claude Code sends it with ANTHROPIC_CUSTOM_HEADERS="X-Trioditis-Key: <key>"."""
    got = headers.get(ROUTER_KEY_HEADER) or ""
    return bool(got) and hmac.compare_digest(got.encode(), expected.encode())


def anthropic_error(status: int, message: str, etype: str = "api_error", headers: dict | None = None) -> JSONResponse:
    """Errors Trioditis itself raises, in Anthropic's shape so Claude Code shows the message. A refusal says
    x-should-retry: false, which Claude Code's gateway guide says it reads."""
    extra = {"x-should-retry": "false"} if 400 <= status < 500 else {}
    return JSONResponse({"type": "error", "error": {"type": etype, "message": message}}, status_code=status,
                        headers={**extra, **(headers or {})} or None)


def router_key_refusal(message: str) -> JSONResponse:
    """A missing, wrong or misplaced router key, answered 407 (Proxy Authentication Required: the router is the
    proxy and it wants its own key), never 401. Claude Code reads any 401 as its own login failing: it refreshes
    the claude.ai login, tries again, and the VS Code chat then opens its sign-in picker, which after a key
    change looped until VS Code was closed (23 Nov 2026). It files 403 as a failed login too. A 407 is shown
    once as written, with no retry and no sign-in (checked with Claude Code 2.1.330, 24 Nov 2026), unless
    CLAUDE_CODE_ENABLE_PROXY_AUTH_HELPER is set, which hands the 407 to the user's proxy helper."""
    return anthropic_error(407, message, "authentication_error",
                           {"proxy-authenticate": f'{ROUTER_KEY_HEADER} realm="trioditis"'})


class Meter:
    """Token counts from the answer as it streams past. Holds one event line at a time, never the body."""

    def __init__(self, content_type: str):
        self.sse = content_type.startswith("text/event-stream")
        self.json = content_type.startswith("application/json")
        self._buf = b""
        self.message_id: str | None = None
        self.model: str | None = None
        self.usage: dict[str, int] = {}

    def feed(self, chunk: bytes) -> None:
        if self.sse:
            self._buf += chunk
            *lines, self._buf = self._buf.split(b"\n")
            if len(self._buf) > METER_LINE_MAX:
                self._buf = b""  # an outsized line (a big tool input) carries no usage; drop it, keep streaming
            for line in lines:
                if line.startswith(b"data:") and (b'"usage"' in line or b'"message_start"' in line):
                    self._event(line[5:])
        elif self.json and len(self._buf) <= METER_JSON_MAX:
            self._buf += chunk

    def finish(self) -> None:
        if self.json and len(self._buf) <= METER_JSON_MAX:
            self._event(self._buf)
        self._buf = b""

    def _event(self, raw: bytes) -> None:
        try:
            o = json.loads(raw)
        except ValueError:
            return
        if not isinstance(o, dict):
            return
        # message_start carries the message (id, model, first usage); message_delta only usage; a non-streamed
        # answer is the message itself.
        msg = o.get("message") if o.get("type") == "message_start" else (o if o.get("type") == "message" else {})
        if not isinstance(msg, dict):
            return
        if isinstance(msg.get("id"), str) and msg["id"].startswith("msg_"):
            self.message_id = msg["id"][:100]
        if isinstance(msg.get("model"), str):
            self.model = _word(msg["model"])
        usage = msg.get("usage") if msg else o.get("usage")
        if not isinstance(usage, dict):
            return
        for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
            v = _count(usage.get(k))
            if v is not None and v >= self.usage.get(k, -1):
                self.usage[k] = v  # counts only grow over a stream; the last report is the total


def mount(app: FastAPI, *, client: httpx.AsyncClient, metrics: MetricsDB, prices: Prices, read_raw: RawReader,
          router_key, caller, on_message=None) -> None:
    """`on_message(headers, raw)`, when given, sees every Messages request's headers and body after the
    body is read and before it goes upstream (the shadow counts, trioditis/shadow.py). It must return
    at once; what it does can't change the request, and an error in it is ignored."""
    base = upstream_base()
    salt: dict[str, str] = {}

    def _salt() -> str:
        if "v" not in salt:
            salt["v"] = load_salt(create=True)
        return salt["v"]

    def record(request: Request, path: str, status: int, meter: Meter, started: float, at: float) -> None:
        if path != MESSAGES:
            return  # count_tokens answers without a model call
        sid = (request.headers.get("x-claude-code-session-id") or "")[:200] or None
        agent = request.headers.get("x-claude-code-agent-id")
        u = meter.usage
        tin, cr, cw, tout = (u.get("input_tokens"), u.get("cache_read_input_tokens"),
                             u.get("cache_creation_input_tokens"), u.get("output_tokens"))
        try:
            if "x-api-key" in request.headers:
                # Billed per token to the key's owner: real spend, so it goes with the API calls and counts
                # toward the caps. The gate never blocks it; Claude Code would only stall.
                usage = None
                if tin is not None and tout is not None:
                    usage = {"prompt_tokens": tin + (cr or 0) + (cw or 0), "completion_tokens": tout}
                    if cr is not None:
                        usage["cached_tokens"] = cr
                cost = prices.cost(meter.model, False, usage) if usage else None
                metrics.record({"route": "passthrough", "model": meter.model or "none",
                                "local": False, "status": status, "latency_ms": int((time.monotonic() - started) * 1000),
                                "usage": usage, "cost": cost, "provider": "anthropic",
                                "unpriced": bool(usage and cost is None and status < 400),
                                "session_fp": fingerprint(sid) if sid else None, "client": caller(request.headers)})
                return
            if status != 200 or not meter.message_id or not meter.model:
                return  # a plan call that failed used nothing worth a row; Claude Code shows the error itself
            s = _salt()
            ev = {"id": "cc-" + _hash(s, meter.message_id), "at": at,
                  "source": "claude-code" if sid else (caller(request.headers) or "anthropic"),
                  "service": "claude", "model": meter.model, "tokens_in": tin, "tokens_out": tout,
                  "cache_read": cr, "cache_write": cw, "tokens_measured": 1, "agent": 1 if agent else 0,
                  "thread_tokens": sum(v or 0 for v in (tin, cr, cw))}
            if sid:
                ev["thread_fp"] = _hash(s, "session\0" + sid, 16)  # the transcript importer's formula
            with metrics.conn:
                merge_events(metrics.conn, [ev])
        except Exception:  # noqa: S110 -- metering is best-effort; it must never break the client's answer
            pass

    async def relay(resp: httpx.Response, meter: Meter, on_done):
        try:
            async for chunk in resp.aiter_raw():  # identity encoding: raw bytes are the events as sent
                meter.feed(chunk)
                yield chunk
        finally:
            await resp.aclose()
            meter.finish()
            on_done()

    async def passthrough(request: Request, path: str):
        started, at = time.monotonic(), time.time()
        key = router_key()
        # The router key in Authorization or x-api-key would go to Anthropic with the request. Refuse
        # it here when it is the value or one word of it: "Bearer <key>", "bearer  <key> ", the bare key.
        # (Not caught: the key inside a longer word, or encoded, as in Basic.)
        carrier = next((name for name in _CREDENTIAL_HEADERS for value in request.headers.getlist(name)
                        if key and _carries(value, key)), None)
        if carrier:
            return router_key_refusal(f"the router key goes in {ROUTER_KEY_HEADER} only "
                                      f"(ANTHROPIC_CUSTOM_HEADERS=\"X-Trioditis-Key: ...\"); it arrived in "
                                      f"{carrier}, which is passed to Anthropic")
        raw = await read_raw(request)
        if raw is None:
            return anthropic_error(413, read_raw.refusal(), "request_too_large")
        if on_message is not None and path == MESSAGES:
            try:
                on_message(request.headers, raw)
            except Exception:  # noqa: S110 -- never let an observer break the client's request
                pass
        headers = [(k, v) for k, v in request.headers.raw
                   if k.lower().decode("latin-1") not in _HOP_REQUEST and not k.lower().startswith(b"x-trioditis-")]
        headers.append((b"accept-encoding", b"identity"))
        url = base + path + (f"?{request.url.query}" if request.url.query else "")
        try:
            req = client.build_request("POST", url, content=raw, headers=headers, timeout=TIMEOUT)
            resp = await client.send(req, stream=True)
        except httpx.HTTPError as e:
            return anthropic_error(502, f"trioditis could not reach {base}: {type(e).__name__}. Detach with "
                                        f"`unset ANTHROPIC_BASE_URL` to go direct.")
        meter = Meter(resp.headers.get("content-type", ""))
        out = StreamingResponse(relay(resp, meter, lambda: record(request, path, resp.status_code, meter, started, at)),
                                status_code=resp.status_code)
        out.raw_headers = [(k, v) for k, v in resp.headers.raw if k.lower().decode("latin-1") not in _HOP_RESPONSE]
        return out

    @app.post(MESSAGES)
    async def messages(request: Request):
        return await passthrough(request, MESSAGES)

    @app.post(COUNT_TOKENS)
    async def count_tokens(request: Request):
        return await passthrough(request, COUNT_TOKENS)


class RawReader:
    """The request body as bytes, read with a cap (a chunked upload has no Content-Length to check first).
    Over the cap it returns None; the refusal text comes from the limit, never from an exception."""

    def __init__(self, max_bytes: int, on_refuse):
        self.max_bytes, self.on_refuse = max_bytes, on_refuse

    async def __call__(self, request: Request) -> bytes | None:
        parts, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > self.max_bytes:
                self.on_refuse(request)
                return None
            parts.append(chunk)
        return b"".join(parts)

    def refusal(self) -> str:
        return f"request body over {self.max_bytes} bytes (limits.max_body_bytes)"
