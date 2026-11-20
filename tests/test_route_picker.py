"""route_picker: a local model picks the route for the first turn of an `auto` conversation."""
import asyncio
import json

import httpx
import pytest

from tests.test_trioditis import BASE, _env, body, make_client  # noqa: F401  (_env: the autouse fixture)
from trioditis.config import ConfigError, parse_config
from trioditis.localcheck import picker_lines
from trioditis.router import RoutePickerClient, SessionPins, decide

pytestmark = pytest.mark.anyio

PICKER_URL = "http://localhost:11434/v1/chat/completions"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def pcfg(picker=None, **over):
    raw = json.loads(json.dumps(BASE))
    raw["routes"]["code"].update(description="Writing or debugging code", tool_keywords=["edit_file"])
    raw["routes"]["quick"]["description"] = "Short questions"
    raw["route_picker"] = {"provider": "local", "model": "qwen2.5-coder:7b", "timeout_ms": 200,
                           **(picker or {})}
    raw.update(over)
    return parse_config(raw)


def reply(text):
    return httpx.Response(200, json={"choices": [{"index": 0, "message": {"role": "assistant", "content": text}}]})


def asker(handler, c=None):
    c = c or pcfg()
    return c, RoutePickerClient(c, httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def never(req):
    raise AssertionError("the picker must not be asked")


# ------------------------------------------------------------------ config

def test_absent_block_is_off():
    raw = json.loads(json.dumps(BASE))
    assert parse_config(raw).route_picker is None


@pytest.mark.parametrize("block, msg", [
    ({"provider": "cloud"}, "local: true"),
    ({"provider": "nope"}, "must name a provider"),
    ({"model": ""}, "route_picker.model"),
    ({"timeout_ms": 0}, "100 to 60000"),
    ({"timeout_ms": "2500"}, "whole number"),
    ({"max_chars": 0}, "at least 1"),
    ({"max_chars": True}, "whole number"),
    ({"temperature": 1}, "unknown key(s) temperature"),
])
def test_config_errors_name_the_fix(block, msg):
    with pytest.raises(ConfigError, match=msg.replace("(", r"\(").replace(")", r"\)")):
        pcfg(block)


def test_routes_need_descriptions():
    raw = json.loads(json.dumps(BASE))
    raw["route_picker"] = {"provider": "local", "model": "m"}
    with pytest.raises(ConfigError, match="route code: needs a description"):
        parse_config(raw)


# ------------------------------------------------------------------ the request

def test_request_is_the_one_the_held_out_test_sent():
    c = pcfg()
    b = RoutePickerClient(c, None).body("fix it")
    assert b == {"model": "qwen2.5-coder:7b", "temperature": 0, "seed": 0, "max_tokens": 8, "messages": [
        {"role": "system", "content": "You sort messages for a router. Routes:\n- code: Writing or debugging code\n"
                                      "- quick: Short questions\nReply with one word: the name of the route "
                                      "that fits the message best."},
        {"role": "user", "content": "Message:\n<<<\nfix it\n>>>\nRoute:"}]}
    assert len(RoutePickerClient(c, None).body("x" * 9000)["messages"][1]["content"]) < 4100


@pytest.mark.parametrize("text, route", [("code", "code"), ("Quick.", "quick"), ("**code**", "code"),
                                         ("I would say quick", "quick"), ("none", None), ("", None)])
def test_first_route_word_wins(text, route):
    assert RoutePickerClient(pcfg(), None).parse(text) == route


# ------------------------------------------------------------------ decisions

async def test_picker_choice_is_served_and_router_route_is_kept():
    seen = []

    def handler(req):
        seen.append((str(req.url), json.loads(req.content)["model"]))
        return reply("code")

    c, p = asker(handler)
    d = await decide(c, body("hello there"), picker=p)  # the router alone: quick (default)
    assert d.route == "code" and d.router_route == "quick"
    assert d.picker["outcome"] == "chose" and d.picker["route"] == "code" and d.picker["instruction"] == 1
    assert seen == [(PICKER_URL, "qwen2.5-coder:7b")]
    assert any("route picker (qwen2.5-coder:7b) chose code" in r and "router alone would pick quick" in r
               for r in d.reasons)
    assert d.as_dict()["router_route"] == "quick" and d.as_dict()["route_picker"]["route"] == "code"


@pytest.mark.parametrize("handler, outcome, why", [
    (lambda req: reply("no idea"), "no_route", "reply named no route"),
    (lambda req: httpx.Response(500, json={"error": "x"}), "error", "HTTPStatusError"),
    (lambda req: httpx.Response(200, text="not json"), "no_route", "reply was not a chat completion"),
])
async def test_failures_fall_back_to_the_router(handler, outcome, why):
    c, p = asker(handler)
    d = await decide(c, body("I have a python bug"), picker=p)
    assert d.route == d.router_route == "code"
    assert d.picker["outcome"] == outcome and d.picker["route"] is None
    assert f"route picker: {why} -> router chose code" in d.reasons


async def test_slow_picker_falls_back_within_the_timeout():
    async def slow(req):
        await asyncio.sleep(2)
        return reply("quick")

    c, p = asker(slow)
    d = await decide(c, body("I have a python bug"), picker=p)
    assert d.route == "code" and d.picker["outcome"] == "timeout"
    assert d.picker["ms"] < 1500
    assert ("route picker: no answer in 0.2 s (left to finish, so a loading model is ready next turn) "
            "-> router chose code") in d.reasons
    p._left.cancel()


async def test_a_late_call_finishes_and_the_next_turn_gets_the_picker():
    # A cold model: the first call outlives timeout_ms. Cancelling it would stop the load, so it is
    # left to finish; while it runs, turns skip the picker instead of queueing behind it.
    calls = []

    async def loading(req):
        calls.append(1)
        if len(calls) == 1:
            await asyncio.sleep(0.5)
        return reply("code")

    c, p = asker(loading)
    d1 = await decide(c, body("hello there"), picker=p)
    assert d1.route == "quick" and d1.picker["outcome"] == "timeout" and p.busy()
    d2 = await decide(c, body("hello there"), picker=p)
    assert d2.route == "quick" and d2.picker["outcome"] == "busy" and len(calls) == 1
    assert "route picker not asked: its last call is still running (the model may be loading)" in d2.reasons
    await p._left  # the late answer is dropped, not served to anyone
    assert not p.busy()
    d3 = await decide(c, body("hello there"), picker=p)
    assert d3.route == "code" and d3.picker["outcome"] == "chose" and len(calls) == 2


async def test_a_late_call_that_fails_is_swallowed():
    async def late_error(req):
        await asyncio.sleep(0.4)
        return httpx.Response(500, json={"error": "x"})

    c, p = asker(late_error)
    d = await decide(c, body("hello there"), picker=p)
    assert d.picker["outcome"] == "timeout"
    with pytest.raises(httpx.HTTPStatusError):
        await p._left
    assert not p.busy()


async def test_tool_keywords_decide_ahead_of_the_picker():
    c, p = asker(never)
    tools = [{"type": "function", "function": {"name": "edit_file", "parameters": {}}}]
    d = await decide(c, body("hello", tools=tools), picker=p)
    assert d.route == "code" and d.picker["outcome"] == "skipped"
    assert "route picker not asked: the request's tools match route code's tool keywords" in d.reasons


async def test_long_prompts_skip_the_picker():
    c, p = asker(never)
    d = await decide(c, body("debug this python " + "x" * 600), picker=p)
    assert d.route == d.router_route == "code" and d.picker["outcome"] == "skipped"
    assert "route picker not asked: the prompt is 618 characters, over max_chars 500" in d.reasons
    c, p = asker(lambda req: reply("code"), pcfg({"max_chars": 1000}))
    d = await decide(c, body("hello " + "x" * 600), picker=p)
    assert d.picker["outcome"] == "chose" and d.route == "code"


async def test_tools_without_a_keyword_match_still_ask_the_picker():
    c, p = asker(lambda req: reply("code"))
    tools = [{"type": "function", "function": {"name": "get_weather", "parameters": {}}}]
    d = await decide(c, body("hello", tools=tools), picker=p)
    assert d.route == "code" and d.picker["outcome"] == "chose"


async def test_forced_routes_explicit_models_and_pinned_turns_never_ask():
    c, p = asker(never)
    assert (await decide(c, body("hi", model="route/quick"), picker=p)).picker is None
    assert (await decide(c, body("hi", model="small"), picker=p)).picker is None
    pins = SessionPins(60)
    pins.set("s1", "code")
    d = await decide(c, body("hi"), picker=p, pins=pins, session_id="s1")
    assert d.route == "code" and d.picker is None and d.router_route is None


async def test_first_turn_choice_is_pinned():
    calls = []

    def handler(req):
        calls.append(1)
        return reply("code")

    c, p = asker(handler)
    pins = SessionPins(60)
    d1 = await decide(c, body("hello"), picker=p, pins=pins, session_id="s1")
    d2 = await decide(c, body("thanks"), picker=p, pins=pins, session_id="s1")
    assert d1.route == d2.route == "code" and len(calls) == 1


async def test_sensitive_prompts_may_go_to_the_local_picker():
    c, p = asker(lambda req: reply("quick"))
    d = await decide(c, body("mail me at someone@example.com"), picker=p)
    assert d.local_only and d.route == "quick" and d.picker["outcome"] == "chose"


async def test_no_picker_block_keeps_the_old_decision():
    raw = json.loads(json.dumps(BASE))
    d = await decide(parse_config(raw), body("hello"))
    assert d.router_route is None and "router_route" not in d.as_dict()


# ------------------------------------------------------------------ HTTP and the log

def test_decision_log_carries_both_routes(tmp_path):
    log = tmp_path / "decisions.jsonl"

    def handler(req):
        sent = json.loads(req.content)
        if sent["model"] == "qwen2.5-coder:7b":
            return reply("code")
        return httpx.Response(200, json={"id": "x", "model": sent["model"], "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "hi"}}]})

    r = make_client(handler, pcfg(logging={"path": str(log)})).post("/v1/chat/completions", json=body("hello"))
    assert r.status_code == 200 and r.headers["x-trioditis-route"] == "code"
    rec = json.loads(log.read_text().splitlines()[-1])
    assert rec["route"] == "code" and rec["router_route"] == "quick"
    assert rec["route_picker"]["outcome"] == "chose" and isinstance(rec["route_picker"]["ms"], int)
    assert "prompt" not in rec and "hello" not in log.read_text()


# ------------------------------------------------------------------ check

def serve(monkeypatch, handler):
    monkeypatch.setattr("trioditis.localcheck._client",
                        lambda **kw: httpx.Client(**{**kw, "transport": httpx.MockTransport(handler)}))


def local_server(listed=("qwen2.5-coder:7b",), answer="quick"):
    def handler(req):
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": m} for m in listed]})
        assert json.loads(req.content)["messages"][1]["content"].startswith("Message:\n<<<\nHello")
        return reply(answer)
    return handler


def ticks(*gaps):
    t, out = [0.0], []
    for g in gaps:
        out += [t[0], t[0] + g]
        t[0] += g
    it = iter(out)
    return lambda: next(it)


def test_check_without_the_block_says_nothing():
    assert picker_lines(parse_config(json.loads(json.dumps(BASE)))) == []


def test_check_server_down_names_the_fallback():
    lines = picker_lines(pcfg())  # conftest: no server answers
    assert lines[0] == "route picker: qwen2.5-coder:7b on local, timeout 0.2 s"
    assert "not answering" in lines[1] and "router's route" in lines[1]


def test_check_missing_model_names_the_pull(monkeypatch):
    serve(monkeypatch, local_server(listed=("llama3.2:3b",)))
    c = pcfg(providers={**BASE["providers"], "ollama": {"base_url": "http://localhost:11434/v1", "local": True}},
             route_picker={"provider": "ollama", "model": "qwen2.5-coder:7b"})
    assert "ollama pull qwen2.5-coder:7b" in picker_lines(c)[1]


def test_check_ready_reports_both_call_times(monkeypatch):
    serve(monkeypatch, local_server())
    lines = picker_lines(pcfg({"timeout_ms": 2500}), clock=ticks(4.0, 1.05))
    assert lines[1] == "  ready: first call 4.00 s (may include loading the model), next call 1.05 s"
    assert len(lines) == 2


def test_check_warns_when_warm_calls_exceed_the_timeout(monkeypatch):
    serve(monkeypatch, local_server())
    lines = picker_lines(pcfg({"timeout_ms": 500}), clock=ticks(4.0, 1.2))
    assert "longer than timeout_ms" in lines[2]


def test_check_reply_without_a_route(monkeypatch):
    serve(monkeypatch, local_server(answer="hello!"))
    assert "named no route" in picker_lines(pcfg())[1]
