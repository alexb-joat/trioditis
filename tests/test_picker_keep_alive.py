"""route_picker.keep_alive_minutes: after each answered picker call, ask Ollama to keep the model loaded.

Absent (the default), nothing changes: the picker sends its one chat request and the server's own
keep-alive applies. Set, the picker also sends an empty POST /api/generate with keep_alive, the call
Ollama's FAQ documents for keeping a model in memory. The stand-in (scripts/fake_ollama.py) shows the
behaviour; its times measure nothing.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from tests.test_picker_speed import LOAD_S, classify, fake_ollama, socket_cfg, stand_ins
from tests.test_route_picker import asker, pcfg, reply
from tests.test_trioditis import BASE, _env  # noqa: F401  (_env: the autouse fixture)
from trioditis.config import ConfigError, parse_config
from trioditis.router import RoutePickerClient

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def stand_in():
    start, stop = stand_ins()
    yield start
    stop()


async def kept(p: RoutePickerClient) -> None:
    """Wait for the keep-alive requests the picker sent in the background."""
    while p._keeping:
        await asyncio.gather(*list(p._keeping))


def recorder(status: int = 200):
    """A mock Ollama that records (path, body) of every request; the model counts as loaded."""
    seen: list[tuple[str, dict | None]] = []

    async def handler(req):
        body = json.loads(req.content) if req.content else None
        seen.append((req.url.path, body))
        if req.url.path == "/api/ps":
            return httpx.Response(200, json={"models": [{"name": "qwen2.5-coder:7b"}]})
        if req.url.path == "/api/generate":
            return httpx.Response(200, json={"model": body["model"], "response": "", "done": True})
        return reply("quick") if status == 200 else httpx.Response(status, json={"error": "failed"})
    handler.ollama = True
    return handler, seen


# ------------------------------------------------------------------ config

def test_absent_means_none():
    assert pcfg().route_picker.keep_alive_minutes is None


def test_a_whole_number_of_minutes_is_kept():
    assert pcfg({"keep_alive_minutes": 30}).route_picker.keep_alive_minutes == 30


@pytest.mark.parametrize("value", [0, -1, 1441, "30m", "30", 1.5, True])
def test_a_bad_value_is_a_plain_config_error(value):
    with pytest.raises(ConfigError, match="keep_alive_minutes must be a whole number from 1 to 1440"):
        pcfg({"keep_alive_minutes": value})


def test_a_provider_that_is_not_ollama_shaped_is_refused():
    raw = json.loads(json.dumps(BASE))
    raw["providers"]["local"]["base_url"] = "http://localhost:8080/api"
    raw["routes"]["code"]["description"] = "code"
    raw["routes"]["quick"]["description"] = "quick"
    raw["route_picker"] = {"provider": "local", "model": "m", "keep_alive_minutes": 30}
    with pytest.raises(ConfigError, match="ends in /v1"):
        parse_config(raw)


def test_the_unknown_key_message_names_the_setting():
    with pytest.raises(ConfigError, match="keep_alive_minutes"):
        pcfg({"keep_alive": "30m"})


@pytest.mark.parametrize("minutes", [1, 30, 1440])
def test_the_request_never_asks_ollama_to_unload(minutes):
    c, p = asker(recorder()[0], pcfg({"keep_alive_minutes": minutes}))
    body = p.keep_alive_body()
    assert body == {"model": "qwen2.5-coder:7b", "keep_alive": f"{minutes}m"}
    assert fake_ollama.keep_alive_seconds(body["keep_alive"]) == minutes * 60 > 0


# ------------------------------------------------------------------ what is sent

async def test_absent_the_picker_sends_only_its_chat():
    handler, seen = recorder()
    c, p = asker(handler, pcfg())
    _, info = await classify(c, p)
    await kept(p)
    assert info["outcome"] == "chose"
    assert [path for path, _ in seen] == ["/v1/chat/completions"]
    assert p.keep_alive_body() is None


async def test_set_the_chat_is_unchanged_and_an_empty_generate_follows_it():
    plain, plain_seen = recorder()
    c0, p0 = asker(plain, pcfg())
    await classify(c0, p0)
    handler, seen = recorder()
    c, p = asker(handler, pcfg({"keep_alive_minutes": 30}))
    _, info = await classify(c, p)
    await kept(p)
    assert info["outcome"] == "chose"
    assert [path for path, _ in seen] == ["/v1/chat/completions", "/api/generate"]
    assert seen[0][1] == plain_seen[0][1]  # the chat request is byte for byte today's
    assert seen[1][1] == {"model": "qwen2.5-coder:7b", "keep_alive": "30m"}  # no prompt: it only loads


async def test_a_failed_chat_sends_no_keep_alive():
    handler, seen = recorder(status=500)
    c, p = asker(handler, pcfg({"keep_alive_minutes": 30}))
    _, info = await classify(c, p)
    await kept(p)
    assert info["outcome"] == "error"
    assert "/api/generate" not in [path for path, _ in seen]


async def test_a_keep_alive_that_fails_changes_nothing_for_the_request():
    async def handler(req):
        if req.url.path == "/api/generate":
            raise httpx.ConnectError("refused", request=req)
        if req.url.path == "/api/ps":
            return httpx.Response(200, json={"models": []})
        return reply("quick")
    handler.ollama = True
    c, p = asker(handler, pcfg({"keep_alive_minutes": 30}))
    _, info = await classify(c, p)
    await kept(p)
    assert info["outcome"] == "chose" and info["route"] == "quick"


# ------------------------------------------------------------------ on the stand-in

async def second_call_after_idle(port: int, **picker) -> tuple[dict, dict]:
    c = socket_cfg(port, **picker)
    async with httpx.AsyncClient() as client:
        p = RoutePickerClient(c, client)
        _, first = await classify(c, p)
        await kept(p)
        await asyncio.sleep(0.5)  # longer than the stand-in's own keep-alive (0.3 s here, 5 minutes in Ollama)
        _, second = await classify(c, p)
        await kept(p)
    return first, second


async def test_without_the_setting_a_call_after_the_idle_time_is_cold_again(stand_in):
    server = stand_in(load_delay=LOAD_S, keep_alive=0.3)
    first, second = await second_call_after_idle(server.server_address[1])
    assert first["outcome"] == "cold" and second["outcome"] == "cold"
    assert server.loads == 2 and server.keep_alive_requests == []


async def test_with_the_setting_the_call_after_the_idle_time_is_warm(stand_in):
    server = stand_in(load_delay=LOAD_S, keep_alive=0.3)
    first, second = await second_call_after_idle(server.server_address[1], keep_alive_minutes=1)
    assert first["outcome"] == "cold" and first["late"]["outcome"] == "chose"
    assert second["outcome"] == "chose" and "late" not in second
    assert server.loads == 1 and server.keep_alive_requests == ["1m", "1m"]


# ------------------------------------------------------------------ the stand-in's keep_alive

@pytest.mark.parametrize("value, seconds", [("30m", 1800.0), ("45s", 45.0), ("1h", 3600.0), (90, 90.0),
                                            ("1.5m", 90.0), ("-1", float("inf")), (-1, float("inf")), (0, 0.0)])
def test_the_stand_in_reads_ollamas_keep_alive_forms(value, seconds):
    assert fake_ollama.keep_alive_seconds(value) == seconds


@pytest.mark.parametrize("value", ["soon", "", True, [1]])
def test_the_stand_in_refuses_other_keep_alive_values(value):
    assert fake_ollama.keep_alive_seconds(value) is None


def test_an_empty_generate_with_zero_unloads_on_the_stand_in(stand_in):
    server = stand_in()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    with httpx.Client() as client:
        assert client.post(f"{base}/api/generate", json={"model": "m", "keep_alive": "10m"}).json()["done"] is True
        assert [m["name"] for m in client.get(f"{base}/api/ps").json()["models"]] == ["m"]
        client.post(f"{base}/api/generate", json={"model": "m", "keep_alive": 0})
        assert client.get(f"{base}/api/ps").json()["models"] == []
        assert client.post(f"{base}/api/generate", json={"model": "m", "prompt": "hi"}).status_code == 400
