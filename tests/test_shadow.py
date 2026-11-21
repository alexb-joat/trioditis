"""Shadow counts (trioditis/shadow.py): Claude Code's first turns classified beside the gate, never served."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.test_gate import CLAUDE_HEADERS, ROUTER_KEY, SSE, TOKEN, sse_response
from tests.test_route_picker import pcfg, reply
from trioditis.app import create_app
from trioditis.config import ConfigError
from trioditis.evallog import format_report, read_log, read_shadow, summarise
from trioditis.shadow import prompt_of
from trioditis_metrics import BudgetCaps, BudgetGuard, MetricsDB, Prices

PROMPT = "fix the failing python test in the parser"
REMINDER = "<system-reminder>\nAs you answer the user's questions, you can use the following context: ...\n</system-reminder>"
TOOLS = [{"name": "Bash", "input_schema": {"type": "object"}}, {"name": "Edit", "input_schema": {"type": "object"}}]


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HLIDSKJALV_HOME", str(tmp_path / "hl"))
    monkeypatch.delenv("TRIODITIS_GATE_UPSTREAM", raising=False)
    return tmp_path


def cc_body(text=PROMPT, tools=TOOLS, reminders=1) -> bytes:
    content = [{"type": "text", "text": REMINDER}] * reminders + [{"type": "text", "text": text}]
    b = {"model": "claude-opus-5-5", "max_tokens": 32, "stream": True,
         "messages": [{"role": "user", "content": content}]}
    if tools is not None:
        b["tools"] = tools
    return json.dumps(b).encode()


def make_app(home: Path, *, shadow=True, picker_answer="code", picker_delay=0.0, timeout_ms=2000):
    calls = {"picker": 0, "upstream": []}

    async def handler(req: httpx.Request):
        if req.url.host == "localhost":
            if req.url.path != "/v1/chat/completions":
                return httpx.Response(404)  # the startup hardware poll asks the local server too
            calls["picker"] += 1
            if picker_delay:
                await asyncio.sleep(picker_delay)
            return reply(picker_answer)
        calls["upstream"].append(req.content)
        return sse_response()

    c = pcfg(picker={"shadow_claude_code": shadow, "timeout_ms": timeout_ms},
             logging={"path": str(home / "logs" / "decisions.jsonl")})
    metrics = MetricsDB(home / "metrics.db")
    app = create_app(c, transport=httpx.MockTransport(handler), metrics=metrics,
                     budget=BudgetGuard(metrics, BudgetCaps()), prices=Prices(home / "prices.json"),
                     hardware_file=home / "hardware.json", accounts_file=home / "accounts.json")
    return app, calls


def post(client, body, **headers):
    return client.post("/v1/messages?beta=true", content=body, headers={**CLAUDE_HEADERS, **headers})


def drained(app, client):
    """Wait until the background shadow tasks have ended (they run on the client's event loop)."""
    for _ in range(300):
        if not app.state.shadow.tasks:
            return
        time.sleep(0.01)
    raise AssertionError("shadow task still running")


def lines(home: Path) -> list[dict]:
    p = home / "logs" / "shadow.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


# ------------------------------------------------------------------ config

def test_off_unless_the_key_says_true():
    assert pcfg().route_picker.shadow_claude_code is False
    assert pcfg(picker={"shadow_claude_code": True}).route_picker.shadow_claude_code is True
    with pytest.raises(ConfigError, match="true or false"):
        pcfg(picker={"shadow_claude_code": "yes"})


# ------------------------------------------------------------------ what counts as the prompt

def test_whole_reminder_blocks_are_left_out_of_the_prompt():
    body = json.loads(cc_body(reminders=2))
    text, message_chars, dropped = prompt_of(body)
    assert text == PROMPT and dropped == 2
    assert message_chars == len("\n".join([REMINDER, REMINDER, PROMPT]))
    mixed = {"messages": [{"role": "user", "content": f"{REMINDER} and then {PROMPT}"}]}
    assert prompt_of(mixed)[0] == f"{REMINDER} and then {PROMPT}"  # a reminder inside typed text stays
    assert prompt_of({"messages": []}) == ("", 0, 0)


# ------------------------------------------------------------------ through the gate

def test_first_turn_logged_once_as_numbers_and_the_request_is_unchanged(home):
    app, calls = make_app(home)
    with TestClient(app, base_url="http://127.0.0.1:8787") as client:
        r = post(client, cc_body())
        assert r.status_code == 200 and r.content == b"".join(SSE)
        post(client, cc_body("a second turn"))  # same session: not counted again
        drained(app, client)
    assert calls["upstream"] == [cc_body(), cc_body("a second turn")]  # bytes as sent
    assert calls["picker"] == 1
    [rec] = lines(home)
    assert rec["shadow"] is True and rec["source"] == "claude-code"
    assert rec["router_route"] == "code"  # keyword "python"
    assert rec["route_picker"]["outcome"] == "chose" and rec["route_picker"]["route"] == "code"
    assert rec["prompt_chars"] == len(PROMPT) and rec["reminder_blocks"] == 1 and rec["tools"] == 2
    assert rec["model"] == "claude-opus-5-5" and rec["session_fp"]
    raw = (home / "logs" / "shadow.jsonl").read_text()
    assert PROMPT not in raw and "failing" not in raw and TOKEN not in raw and ROUTER_KEY not in raw
    assert CLAUDE_HEADERS["x-claude-code-session-id"] not in raw
    assert (home / "logs" / "shadow.jsonl").stat().st_mode & 0o777 == 0o600


def test_side_calls_and_subagents_are_not_the_first_turn(home):
    app, calls = make_app(home)
    with TestClient(app, base_url="http://127.0.0.1:8787") as client:
        post(client, cc_body("write a title for this chat", tools=None))   # a side call: no tools
        drained(app, client)
        assert lines(home) == []
        post(client, cc_body(), **{"x-claude-code-agent-id": "agent-1"})    # a subagent
        drained(app, client)
        assert lines(home) == []
        post(client, cc_body())                                             # the main turn
        drained(app, client)
    assert len(lines(home)) == 1 and lines(home)[0]["prompt_chars"] == len(PROMPT)


def test_off_by_default_and_without_a_session_id(home):
    app, calls = make_app(home, shadow=False)
    with TestClient(app, base_url="http://127.0.0.1:8787") as client:
        post(client, cc_body())
        drained(app, client)
    assert calls["picker"] == 0 and lines(home) == []

    app, calls = make_app(home)
    headers = {k: v for k, v in CLAUDE_HEADERS.items() if k != "x-claude-code-session-id"}
    with TestClient(app, base_url="http://127.0.0.1:8787") as client:
        client.post("/v1/messages", content=cc_body(), headers=headers)
        client.post("/v1/messages/count_tokens", content=cc_body(), headers=CLAUDE_HEADERS)
        drained(app, client)
    assert calls["picker"] == 0 and lines(home) == []


def test_a_slow_picker_never_delays_the_answer(home):
    app, calls = make_app(home, picker_delay=1.5, timeout_ms=300)
    with TestClient(app, base_url="http://127.0.0.1:8787") as client:
        t = time.monotonic()
        r = post(client, cc_body())
        took = time.monotonic() - t
        assert r.status_code == 200 and r.content == b"".join(SSE)
        assert took < 1.0, took  # the picker is still thinking; the answer is already back
        drained(app, client)
    [rec] = lines(home)
    assert rec["route_picker"]["outcome"] == "timeout"  # as a served request would have had it


def test_a_long_prompt_is_counted_as_skipped(home):
    app, calls = make_app(home)
    with TestClient(app, base_url="http://127.0.0.1:8787") as client:
        post(client, cc_body("x " * 300))
        drained(app, client)
    [rec] = lines(home)
    assert calls["picker"] == 0 and rec["route_picker"]["outcome"] == "skipped" and rec["prompt_chars"] == 600


def test_a_broken_body_is_passed_on_and_counted_as_nothing(home):
    app, calls = make_app(home)
    with TestClient(app, base_url="http://127.0.0.1:8787") as client:
        r = post(client, b"{not json")
        drained(app, client)
    assert r.status_code == 200 and calls["upstream"] == [b"{not json"] and lines(home) == []


# ------------------------------------------------------------------ eval-routes --log

def test_eval_routes_log_counts_shadow_lines_without_a_cost(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "decisions.jsonl").write_text(json.dumps({"route": "quick", "model": "small", "status": 200}) + "\n")
    shadow = [{"shadow": True, "ts": "2026-11-21T10:00:00+0100", "router_route": r,
               "route_picker": {"model": "qwen2.5-coder:7b", "instruction": 1, "outcome": "chose", "route": p, "ms": 900}}
              for r, p in [("quick", "code"), ("code", "code")]]
    shadow.append({"shadow": True, "router_route": "quick", "route_picker": {"outcome": "skipped"}})
    shadow.append({"router_route": "quick", "route_picker": {"outcome": "chose"}})  # not marked shadow: ignored
    (logs / "shadow.jsonl").write_text("".join(json.dumps(x) + "\n" for x in shadow) + "not json\n")
    data = read_log(logs / "decisions.jsonl")
    read_shadow(data, logs / "decisions.jsonl")
    assert data.shadow == 3 and data.unreadable == 1 and len(data.files) == 2
    c = pcfg()
    s = summarise(c, data.records, Prices(tmp_path / "prices.json"), data.served)
    assert s.n == 3 and s.chose == 2 and s.agreed == 1 and s.pairs[("quick", "code")] == 1
    assert s.cost.n == 0  # nothing was served, so there is nothing to price
    report = "\n".join(format_report(data, s, "config.yaml", "2026-11-21", True))
    assert "3 of the 3 are shadow counts" in report and "shadow.jsonl" in report
