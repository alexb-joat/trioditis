"""The route picker's misses, reproduced: each outcome the shadow log shows for a slow or cold picker.

The shadow log counts first turns as `cold` (no answer in time while the model was still loading),
`timeout` (the model was loaded but slow), `skipped` (the prompt was over max_chars) and `busy`
(another picker call was still running). These tests drive the path the shadow count calls,
`_classify_with_picker(..., late=True)`, and get each of them on purpose: in process with a mock
provider for speed, and once over a real socket against scripts/fake_ollama.py. Every delay here is a
test setting; none of the times measures anything.
"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

from tests.test_route_picker import asker, never, pcfg, reply
from tests.test_trioditis import _env  # noqa: F401  (the autouse fixture: CLOUD_KEY for the base config)
from trioditis.config import parse_config
from trioditis.router import RoutePickerClient, _classify_with_picker

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import fake_ollama  # noqa: E402

pytestmark = pytest.mark.anyio

LOAD_S = 0.6      # a test model load: six times timeout_ms (100 ms), so the first call misses it


@pytest.fixture
def anyio_backend():
    return "asyncio"


def fast(**picker):
    return pcfg({"timeout_ms": 100, **picker})


class Loading:
    """A mock Ollama whose model loads on the first chat: /api/ps lists it only once the load is over,
    and a chat that arrives during the load waits for it, as Ollama queues it."""

    def __init__(self, load_s: float = LOAD_S, warm_s: float = 0.0, model: str = "qwen2.5-coder:7b"):
        self.load_s, self.warm_s, self.model = load_s, warm_s, model
        self.ready: float | None = None
        self.chats = 0

    def loaded(self) -> bool:
        return self.ready is not None and time.monotonic() >= self.ready

    async def chat(self, req):
        self.chats += 1
        if self.ready is None:
            self.ready = time.monotonic() + self.load_s
        wait = self.ready - time.monotonic()
        await asyncio.sleep(max(wait, 0.0) + self.warm_s)
        return reply("quick")

    def handler(self):
        async def handler(req):
            if req.url.path == "/api/ps":
                return httpx.Response(200, json={"models": [{"name": self.model, "model": self.model}]
                                                 if self.loaded() else []})
            return await self.chat(req)
        handler.ollama = True
        return handler


async def classify(c, p, text="what is a good name for a cat"):
    _, router, info = await _classify_with_picker(c, text, None, p, [], late=True)
    return router, info


# ------------------------------------------------------------------ in process

async def test_a_cold_model_is_counted_cold_and_its_late_answer_comes_after_the_load():
    server = Loading()
    c, p = asker(server.handler(), fast())
    router, info = await classify(c, p)
    assert info["outcome"] == "cold" and info["route"] is None
    assert 100 <= info["ms"] < LOAD_S * 1000
    assert info["late"]["outcome"] == "chose" and info["late"]["route"] == "quick"
    assert info["late"]["ms"] >= LOAD_S * 1000 * 0.9
    assert router == "quick"


async def test_the_turn_after_the_load_is_answered_in_time():
    server = Loading()
    c, p = asker(server.handler(), fast())
    await classify(c, p)  # cold: loads the model
    _, info = await classify(c, p)
    assert info["outcome"] == "chose" and info["route"] == "quick" and "late" not in info
    assert server.chats == 2


async def test_a_loaded_but_slow_model_is_a_timeout_not_a_cold_start():
    server = Loading(load_s=0.0, warm_s=0.3)
    server.ready = time.monotonic()  # already in memory
    c, p = asker(server.handler(), fast())
    _, info = await classify(c, p)
    assert info["outcome"] == "timeout"
    assert info["late"]["outcome"] == "chose" and info["late"]["ms"] >= 300 * 0.9


async def test_a_prompt_over_max_chars_is_skipped_and_the_picker_is_not_asked():
    c, p = asker(never, fast(max_chars=200))
    _, info = await classify(c, p, "x " * 100 + "y")  # 201 characters
    assert info["outcome"] == "skipped" and info["ms"] is None and "late" not in info


async def test_a_prompt_at_max_chars_is_still_asked():
    server = Loading(load_s=0.0)
    c, p = asker(server.handler(), fast(max_chars=200))
    _, info = await classify(c, p, "x" * 200)
    assert info["outcome"] == "chose" and server.chats == 1


async def test_an_empty_prompt_is_skipped():
    c, p = asker(never, fast())
    _, info = await classify(c, p, "   ")
    assert info["outcome"] == "skipped"


async def test_a_second_turn_during_a_load_is_busy_and_does_not_queue_behind_it():
    server = Loading()
    c, p = asker(server.handler(), fast())
    first = asyncio.ensure_future(classify(c, p))
    while not p.busy():
        await asyncio.sleep(0.01)
    t0 = time.monotonic()
    _, info = await classify(c, p, "and another question")
    assert info["outcome"] == "busy" and info["ms"] is None
    assert time.monotonic() - t0 < LOAD_S  # it did not wait for the load
    _, cold = await first
    assert cold["outcome"] == "cold" and server.chats == 1


# ------------------------------------------------------------------ over a real socket

def socket_cfg(port: int, **picker):
    route = {"models": ["small"]}
    return parse_config({
        "providers": {"local": {"base_url": f"http://127.0.0.1:{port}/v1", "local": True}},
        "models": {"small": {"provider": "local", "id": "tiny:1b"}},
        "routes": {"code": {**route, "description": "Writing or debugging code"},
                   "reasoning": {**route, "description": "Hard problems"},
                   "writing": {**route, "description": "Prose"},
                   "quick": {**route, "description": "Short questions"}},
        "default_route": "quick", "logging": {"path": None},
        "route_picker": {"provider": "local", "model": "tiny:1b", "timeout_ms": 100, **picker}})


@pytest.fixture
def stand_in():
    servers = []

    def start(**kw):
        server = fake_ollama.make_server(0, **kw)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append((server, thread))
        return server

    yield start
    for server, thread in servers:
        server.shutdown()
        server.server_close()
        thread.join(5)


async def test_the_stand_in_gives_cold_then_warm_then_cold_after_its_keep_alive(stand_in):
    server = stand_in(load_delay=LOAD_S, keep_alive=0.3)
    port = server.server_address[1]
    c = socket_cfg(port)
    async with httpx.AsyncClient() as client:
        p = RoutePickerClient(c, client)
        assert (await client.get(f"http://127.0.0.1:{port}/api/ps")).json() == {"models": []}

        _, info = await classify(c, p)
        assert info["outcome"] == "cold" and info["late"]["outcome"] == "chose"
        assert info["late"]["ms"] >= LOAD_S * 1000 * 0.9
        listed = (await client.get(f"http://127.0.0.1:{port}/api/ps")).json()["models"]
        assert [m["name"] for m in listed] == ["tiny:1b"] and await p.loaded() is True

        _, info = await classify(c, p)
        assert info["outcome"] == "chose" and "late" not in info

        await asyncio.sleep(0.5)  # past the stand-in's keep-alive: the model is dropped
        assert await p.loaded() is False
        _, info = await classify(c, p)
        assert info["outcome"] == "cold"
    assert server.loads == 2


# ------------------------------------------------------------------ the stand-in itself

def test_defaults_are_unchanged_loads_are_instant_and_last_five_minutes(stand_in):
    server = stand_in()
    assert (server.load_delay, server.keep_alive, server.per_char) == (0.0, 300.0, 0.0)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    with httpx.Client() as client:
        t0 = time.monotonic()
        r = client.post(f"{base}/v1/chat/completions", json={"model": "tiny:1b", "messages": [
            {"role": "user", "content": "hello"}]})
        assert r.status_code == 200 and time.monotonic() - t0 < 1.0
        listed = client.get(f"{base}/api/ps").json()["models"]
    assert [m["name"] for m in listed] == ["tiny:1b"]
    assert set(listed[0]) == {"name", "model", "size", "digest", "expires_at", "size_vram"}


def test_api_ps_is_not_found_on_a_server_that_is_not_ollama(stand_in):
    server = stand_in(mode="no-api")
    with httpx.Client() as client:
        assert client.get(f"http://127.0.0.1:{server.server_address[1]}/api/ps").status_code == 404


def test_per_char_slows_a_warm_chat(stand_in):
    server = stand_in(per_char=0.002)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    with httpx.Client() as client:
        t0 = time.monotonic()
        client.post(f"{base}/v1/chat/completions", json={"model": "m", "messages": [
            {"role": "user", "content": "x" * 200}]})
        assert time.monotonic() - t0 >= 0.4 * 0.9


def test_help_lists_the_new_options(capsys):
    with pytest.raises(SystemExit):
        fake_ollama.main(["--help"])
    out = capsys.readouterr().out
    for flag in ("--load-delay", "--keep-alive", "--per-char"):
        assert flag in out


@pytest.mark.parametrize("flag", ["--load-delay", "--keep-alive", "--per-char"])
def test_a_negative_delay_is_refused(flag, capsys):
    with pytest.raises(SystemExit):
        fake_ollama.main(["--port", "1", flag, "-1"])
    assert "must be 0 or more" in capsys.readouterr().err


def test_the_stand_in_still_binds_only_the_loopback_address(stand_in):
    assert stand_in().server_address[0] == "127.0.0.1"


def test_no_prompt_reaches_the_stand_ins_memory(stand_in):
    server = stand_in()
    with httpx.Client() as client:
        client.post(f"http://127.0.0.1:{server.server_address[1]}/v1/chat/completions",
                    json={"model": "m", "messages": [{"role": "user", "content": "marker-7f3a"}]})
    assert "marker-7f3a" not in json.dumps({k: v for k, v in server.models.items()})
