"""route_picker.long_prompts: a prompt over max_chars skips the picker (skip, the default), or the picker
reads its first max_chars characters (cut). The router reads the whole prompt either way. The wait for a cut
prompt is that of a max_chars prompt; how often a cut prompt gets the right route is not measured, and
nothing here measures it."""
from __future__ import annotations

import json

import httpx
import pytest

from tests.test_route_picker import asker, never, pcfg, reply
from tests.test_trioditis import _env  # noqa: F401  (the autouse fixture: CLOUD_KEY for the base config)
from trioditis.config import ConfigError
from trioditis.evallog import _limits
from trioditis.router import _classify_with_picker

pytestmark = pytest.mark.anyio

# 600 characters: a neutral start, and the base config's code keyword ("python") only past 500.
LONG = "please look at this " * 30
LONG = LONG[:550] + " the python script ".ljust(50, ".")
assert len(LONG) == 600 and "python" not in LONG[:500]


@pytest.fixture
def anyio_backend():
    return "asyncio"


def recorder(route="quick"):
    """A picker server that keeps the user message of each chat it answers."""
    asked: list[str] = []

    def handler(req: httpx.Request):
        asked.append(json.loads(req.content)["messages"][-1]["content"])
        return reply(route)
    return handler, asked


async def classify(c, p, text=LONG):
    reasons: list[str] = []
    served, router, info = await _classify_with_picker(c, text, None, p, reasons, late=True)
    return served, router, info, reasons


# ------------------------------------------------------------------ config

def test_absent_means_skip_as_before():
    assert pcfg().route_picker.long_prompts == "skip"


@pytest.mark.parametrize("value", ["skip", "cut"])
def test_skip_and_cut_are_kept(value):
    assert pcfg({"long_prompts": value}).route_picker.long_prompts == value


@pytest.mark.parametrize("value", ["Cut", "truncate", "", None, True, 1])
def test_any_other_value_is_refused(value):
    with pytest.raises(ConfigError, match="long_prompts must be skip .* or cut"):
        pcfg({"long_prompts": value})


def test_the_unknown_key_message_names_it():
    with pytest.raises(ConfigError, match="keep_alive_minutes, long_prompts"):
        pcfg({"long_prompt": "cut"})


def test_the_shipped_limits_stay_as_they_were():
    p = pcfg({"timeout_ms": 2500}).route_picker
    assert (p.timeout_ms, p.max_chars, p.long_prompts) == (2500, 200, "skip")


# ------------------------------------------------------------------ what the picker is given

@pytest.mark.parametrize("max_chars", [200, 500])
async def test_a_600_character_prompt_is_still_skipped_without_the_setting(max_chars):
    c, p = asker(never, pcfg({"max_chars": max_chars}))
    _, _, info, reasons = await classify(c, p)
    assert info["outcome"] == "skipped" and info["ms"] is None and "cut" not in info
    assert f"route picker not asked: the prompt is 600 characters, over max_chars {max_chars}" in reasons


@pytest.mark.parametrize("max_chars", [200, 500])
async def test_with_cut_the_picker_is_asked_with_the_first_max_chars_characters(max_chars):
    handler, asked = recorder()
    c, p = asker(handler, pcfg({"max_chars": max_chars, "long_prompts": "cut"}))
    served, _, info, reasons = await classify(c, p)
    assert asked == [f"Message:\n<<<\n{LONG[:max_chars]}\n>>>\nRoute:"]
    assert info["outcome"] == "chose" and served == "quick"
    assert info["cut"] == {"from": 600, "to": max_chars}
    assert f"route picker reads the first {max_chars} of 600 characters (long_prompts: cut)" in reasons


async def test_the_router_still_reads_the_whole_prompt():
    # The keyword sits past the cut: the picker never sees it, the router does.
    handler, asked = recorder("quick")
    c, p = asker(handler, pcfg({"max_chars": 500, "long_prompts": "cut"}))
    served, router, info, _ = await classify(c, p)
    assert "python" not in asked[0]
    assert router == "code" and served == "quick"


async def test_a_prompt_within_max_chars_is_sent_whole_and_not_marked_cut():
    handler, asked = recorder()
    c, p = asker(handler, pcfg({"max_chars": 500, "long_prompts": "cut"}))
    text = LONG[:500]
    _, _, info, reasons = await classify(c, p, text)
    assert asked == [f"Message:\n<<<\n{text}\n>>>\nRoute:"]
    assert "cut" not in info and not any("long_prompts" in r for r in reasons)


async def test_cut_changes_nothing_for_an_empty_prompt():
    c, p = asker(never, pcfg({"long_prompts": "cut"}))
    _, _, info, _ = await classify(c, p, "   ")
    assert info["outcome"] == "skipped"


# ------------------------------------------------------------------ what the logs say

def test_the_eval_label_names_the_cut_and_keeps_the_old_label_otherwise():
    base = {"timeout_ms": 5000, "max_chars": 500, "keep_alive_minutes": None}
    assert _limits(base) == "timeout_ms 5000, max_chars 500, keep-alive off"
    assert _limits({**base, "long_prompts": "skip"}) == "timeout_ms 5000, max_chars 500, keep-alive off"
    assert _limits({**base, "long_prompts": "cut"}) == "timeout_ms 5000, max_chars 500, keep-alive off, long prompts cut"
