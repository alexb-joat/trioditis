"""Property tests: the router's safety rules hold for any config and request Hypothesis can build, not only the
hand-picked ones in test_trioditis.py. Each test states one rule; Hypothesis searches for a counterexample and
shrinks it to the smallest config and request that break the rule."""
import asyncio
import os
from unittest import mock

from hypothesis import event, given, settings
from hypothesis import strategies as st
from starlette.datastructures import Headers

from trioditis.config import Privacy, parse_config
from trioditis.gate import ROUTER_KEY_HEADER, key_ok
from trioditis.router import RoutingError, decide, estimate_tokens
from trioditis.security import check_bearer, detect_sensitive

KEY_ENV = "TRIODITIS_PROPTEST_CLOUD_KEY"  # set only while an example runs (mock.patch.dict), never left behind
PROVIDERS = {
    "local": {"base_url": "http://localhost:11434/v1", "local": True},
    "cloud": {"base_url": "https://api.example.com/v1", "api_key_env": KEY_ENV},
    "cloud_nokey": {"base_url": "https://api.example.org/v1", "api_key_env": "TRIODITIS_PROPTEST_NEVER_SET"},
}
MODEL_NAMES = [f"m{i}" for i in range(6)]
WORDS = ["python", "bug", "draft", "summary", "hello", "```"]
SENSITIVE = [  # one sample per built-in detector it names; test_sensitive_samples_fire keeps them honest
    "mail me at someone@example.com",
    "card 4111 1111 1111 1111",
    "SSN 123-45-6789",
    "password = hunter2",
    "-----BEGIN RSA PRIVATE KEY-----",
]
PAD = "x" * 8000  # about 2,000 estimated tokens: too big for the 2,000-token models, fine for 8,192
SOMETIMES = st.sampled_from([False, False, True])  # weights keep most requests servable, so rules get exercised


@st.composite
def configs(draw, on_no_local="reject"):
    names = draw(st.lists(st.sampled_from(MODEL_NAMES), min_size=1, max_size=6, unique=True))
    models = {
        n: {
            "provider": draw(st.sampled_from(["local", "cloud", "cloud", "cloud_nokey"])),
            "id": f"upstream-{n}",
            "capabilities": draw(st.sampled_from([[], ["tools"], ["vision"], ["vision", "tools"], ["vision", "tools"]])),
            "context_tokens": draw(st.sampled_from([500, 2000, 8192, 200000, 200000])),
        }
        for n in names
    }
    routes = {
        f"r{i}": {
            "models": draw(st.lists(st.sampled_from(names), min_size=1, max_size=len(names), unique=True)),
            "keywords": draw(st.lists(st.sampled_from(WORDS[:4]), unique=True, max_size=2)),
            "local_only": draw(SOMETIMES),
        }
        for i in range(draw(st.integers(1, 3)))
    }
    return {
        "providers": PROVIDERS,
        "models": models,  # a model no route lists is "by name only": fallback must never reach it
        "routes": routes,
        "default_route": draw(st.sampled_from(sorted(routes))),
        "privacy": {"on_no_local_model": on_no_local},
        "limits": {"max_fallbacks": draw(st.integers(0, 3))},
        "logging": {"path": None},
    }


@st.composite
def requests(draw, raw_cfg):
    text = " ".join(draw(st.lists(st.sampled_from(WORDS), max_size=4)))
    secret = draw(st.sampled_from([None] * 7 + SENSITIVE))
    if secret:
        text += " " + secret
    if draw(SOMETIMES):
        text += " " + PAD
    images = draw(SOMETIMES)
    content = [{"type": "text", "text": text}]
    if images:
        content.append({"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}})
    body = {"messages": [{"role": "user", "content": content}]}
    tools = draw(SOMETIMES)
    if tools:
        body["tools"] = [{"type": "function", "function": {"name": "edit_file", "parameters": {}}}]
    model_names = sorted(raw_cfg["models"])
    body["model"] = draw(st.sampled_from(
        ["auto", "nope"]
        + [f"route/{r}" for r in sorted(raw_cfg["routes"])]
        + model_names
        + [raw_cfg["models"][n]["id"] for n in model_names]
    ))
    return {"body": body, "secret": secret, "images": images, "tools": tools}


def cloud_key():
    """The "cloud" provider's key, present for the whole example (checks call available() too)."""
    return mock.patch.dict(os.environ, {KEY_ENV: "test-key"})


def route(raw_cfg, req):
    """(config, decision or None, RoutingError or None)."""
    cfg = parse_config(raw_cfg)
    try:
        decision = asyncio.run(decide(cfg, req["body"]))
    except RoutingError as err:
        event(f"refused {err.status}")
        return cfg, None, err
    clouds = [n for n in decision.candidates if not cfg.providers[cfg.models[n].provider].local]
    event(("sensitive, " if decision.sensitive else "") + ("cloud candidate" if clouds else "local only"))
    return cfg, decision, None


def test_sensitive_samples_fire():
    for sample in SENSITIVE:
        assert detect_sensitive(sample, Privacy()), sample


@settings(deadline=None, max_examples=200)
@given(data=st.data())
def test_sensitive_content_reaches_only_local_models(data):
    with cloud_key():
        raw_cfg = data.draw(configs())
        req = data.draw(requests(raw_cfg))
        cfg, decision, err = route(raw_cfg, req)
        if err:
            assert err.status in (404, 422, 503)
            if req["secret"]:
                assert err.status != 503, "sensitive content with no local model must be refused with 422 (reject)"
            return
        if req["secret"]:
            assert decision.sensitive and decision.local_only
        if decision.sensitive:
            for name in decision.candidates:
                assert cfg.providers[cfg.models[name].provider].local, f"sensitive request offered to cloud model {name}"


@settings(deadline=None, max_examples=200)
@given(data=st.data())
def test_candidates_fit_the_request_and_nothing_comes_by_surprise(data):
    with cloud_key():
        raw_cfg = data.draw(configs())
        req = data.draw(requests(raw_cfg))
        cfg, decision, err = route(raw_cfg, req)
        if err:
            return  # refusals are the first test's business
        cands = decision.candidates
        assert cands and len(cands) == len(set(cands)) and len(cands) <= 1 + cfg.limits.max_fallbacks
        tokens = estimate_tokens(req["body"])
        in_routes = {m for r in cfg.routes.values() for m in r.models}
        for name in cands:
            model = cfg.models[name]
            provider = cfg.providers[model.provider]
            assert provider.available(), f"{name}: provider has no key"
            assert not req["images"] or "vision" in model.capabilities, f"{name}: image sent to a model without vision"
            assert not req["tools"] or "tools" in model.capabilities, f"{name}: tools sent to a model without tools"
            assert tokens <= model.context_tokens, f"{name}: {tokens} tokens exceed {model.context_tokens}"
            if decision.route != "explicit":
                assert name in in_routes or provider.local, f"{name}: by-name-only model reached without being asked"
        if decision.route == "explicit":
            asked = req["body"]["model"]
            assert cands == [asked if asked in cfg.models else next(n for n, m in cfg.models.items()
                                                                    if m.upstream_id == asked)]


# Header values arrive as latin-1 text (Starlette decodes the raw bytes that way).
header_text = st.text(st.characters(max_codepoint=255, exclude_categories=["Cs"]), max_size=40)


@given(expected=header_text.filter(bool), extra=header_text.filter(bool), other=header_text, data=st.data())
def test_router_key_passes_only_on_exact_match(expected, extra, other, data):
    cut = data.draw(st.integers(0, len(expected)))
    # Every example tries each near miss: random text alone almost never hits a prefix or a case change.
    for sent in (expected, expected[:-1], expected[:cut], expected + extra, expected.swapcase(), f" {expected}", other):
        if not all(ord(c) < 256 for c in sent):  # "ÿ".swapcase() is "Ÿ": no header can carry it
            continue
        assert key_ok(Headers(headers={ROUTER_KEY_HEADER: sent}), expected) == (sent == expected), repr(sent)
        assert check_bearer(f"Bearer {sent}", expected) == (sent == expected), repr(sent)
    assert key_ok(Headers(headers={"authorization": f"Bearer {expected}"}), expected) is False
    assert check_bearer(None, expected) is False
