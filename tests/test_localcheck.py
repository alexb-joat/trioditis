"""`check` asks local servers (never cloud) which models they have; the 502 text says what to do."""
from __future__ import annotations

import json

import httpx
import pytest

from tests.test_trioditis import BASE, _env, body, cfg, make_client  # noqa: F401  (_env: the autouse fixture)


def _fake_network(monkeypatch, handler):
    """Every probe goes to `handler`; returns the list of URLs asked."""
    asked: list[str] = []

    def answer(req):
        asked.append(str(req.url))
        return handler(req)

    monkeypatch.setattr("trioditis.localcheck._client",
                        lambda **kw: httpx.Client(**{**kw, "transport": httpx.MockTransport(answer)}))
    return asked


def _run_check(tmp_path, capsys):
    from trioditis.__main__ import main
    f = tmp_path / "config.yaml"
    f.write_text(json.dumps(BASE), encoding="utf-8")  # JSON is YAML
    main(["--config", str(f), "check"])
    return capsys.readouterr().out


def _rows(out):
    return {ln.split()[0]: ln for ln in out.splitlines() if ln.startswith("  ") and len(ln.split()) > 2}


def test_check_local_server_answers_and_lists_the_model(tmp_path, capsys, monkeypatch):
    asked = _fake_network(monkeypatch, lambda r: httpx.Response(
        200, json={"data": [{"id": "llama3.2:3b"}, {"id": "qwen-coder:latest"}]}))
    out = _run_check(tmp_path, capsys)
    assert asked == ["http://localhost:11434/v1/models"]  # once, to the local provider only
    rows = _rows(out)
    assert rows["small"].endswith("ready")
    assert rows["coder"].endswith("ready")  # "qwen-coder" is "qwen-coder:latest" for Ollama
    assert rows["big"].endswith("key set (not tested)") and "cloud-secret" not in out
    assert "2 of 3 models confirmed ready" in out
    assert max(len(ln) for ln in out.splitlines()) <= 100


def test_check_names_the_fix_for_a_model_the_server_lacks(tmp_path, capsys, monkeypatch):
    _fake_network(monkeypatch, lambda r: httpx.Response(200, json={"data": [{"id": "llama3.2:3b"}]}))
    rows = _rows(_run_check(tmp_path, capsys))
    assert rows["small"].endswith("ready")
    assert rows["coder"].endswith("not on this server: ollama pull qwen-coder")


def test_check_does_not_claim_a_miss_it_cannot_match(monkeypatch):
    from trioditis.localcheck import Listing, has_model, local_states
    cfg_ = cfg(providers={**BASE["providers"], "local": {"base_url": "http://localhost:8080/v1", "local": True}})
    assert not has_model(cfg_.providers["local"], Listing(answered=True, ids={"other"}), "qwen-coder")
    _fake_network(monkeypatch, lambda r: httpx.Response(200, json={"data": [{"id": "other"}]}))
    states, notes = local_states(cfg_)
    assert states["coder"] == "server answers; model not checked" and notes == []  # not Ollama: no "pull"
    _fake_network(monkeypatch, lambda r: httpx.Response(404, text="nope"))
    assert local_states(cfg_)[0]["coder"] == "server answers (HTTP 404); model not checked"
    _fake_network(monkeypatch, lambda r: httpx.Response(200, text="<html>"))
    assert local_states(cfg_)[0]["coder"] == "server answers; model not checked"


def test_check_says_the_server_did_not_answer_with_its_url(tmp_path, capsys, monkeypatch):
    def down(req):
        raise httpx.ConnectError("refused")

    asked = _fake_network(monkeypatch, down)
    out = _run_check(tmp_path, capsys)
    assert len(asked) == 1  # one try per provider, however many models use it
    assert _rows(out)["small"].endswith("server not answering: start it (ollama serve)")
    assert "local did not answer at http://localhost:11434/v1" in " ".join(out.split())
    assert "0 of 3 models confirmed ready" in out
    assert max(len(ln) for ln in out.splitlines()) <= 100


def test_check_names_the_missing_key_of_a_local_server_and_does_not_ask_it(tmp_path, capsys, monkeypatch):
    from trioditis.__main__ import main
    asked = _fake_network(monkeypatch, lambda r: httpx.Response(200, json={"data": []}))
    monkeypatch.delenv("LOCAL_KEY", raising=False)
    conf = json.loads(json.dumps(BASE))
    conf["providers"]["local"]["api_key_env"] = "LOCAL_KEY"
    f = tmp_path / "config.yaml"
    f.write_text(json.dumps(conf), encoding="utf-8")
    main(["--config", str(f), "check"])
    rows = _rows(capsys.readouterr().out)
    assert rows["small"].endswith("missing $LOCAL_KEY") and rows["coder"].endswith("missing $LOCAL_KEY")
    assert asked == []


def test_check_never_contacts_a_cloud_provider(tmp_path, capsys, monkeypatch):
    asked = _fake_network(monkeypatch, lambda r: httpx.Response(200, json={"data": []}))
    out = _run_check(tmp_path, capsys)
    assert asked and all(u.startswith("http://localhost:11434/") for u in asked)
    assert not any("example.com" in u for u in asked)
    assert "key set (not tested)" in out


def test_502_for_a_local_connection_error_says_what_to_do():
    def handler(req):
        raise httpx.ConnectError("down")

    r = make_client(handler).post("/v1/chat/completions", json=body("hello"))
    assert r.status_code == 502 and r.json()["error"]["type"] == "upstream_error"
    msg = r.json()["error"]["message"]
    assert msg.startswith("all candidate models failed: small: ConnectError.")
    assert "[" not in msg and "{" not in msg  # text, not a Python list
    assert "http://localhost:11434/v1" in msg and "start it" in msg and "trioditis check" in msg


def test_502_for_another_error_has_readable_attempts_and_no_server_hint():
    def handler(req):
        return httpx.Response(500, json={"error": "boom"})

    r = make_client(handler).post("/v1/chat/completions", json=body("python bug"))  # big, then coder
    assert r.status_code == 502
    assert r.json()["error"]["message"] == "all candidate models failed: big: HTTP 500, coder: HTTP 500."


@pytest.mark.parametrize("attempts,text", [
    ([{"model": "m", "error": "budget_blocked", "reason": "daily cap"}], "m: budget_blocked (daily cap)"),
    ([{"model": "a", "status": 429, "retry_after": 3}, {"model": "b", "error": "ReadTimeout"}],
     "a: HTTP 429, b: ReadTimeout"),
])
def test_attempts_read_as_text(attempts, text):
    from trioditis.localcheck import attempts_text
    assert attempts_text(attempts) == text


def test_429_lists_the_attempts_as_text_and_keeps_its_headers():
    c = make_client(lambda req: httpx.Response(429, headers={"retry-after": "7"}, json={"error": "slow down"}))
    r = c.post("/v1/chat/completions", json=body("hello"))
    assert r.status_code == 429 and r.headers["retry-after"] == "7"
    assert r.json()["error"]["type"] == "rate_limited"
    assert r.json()["error"]["message"] == "rate-limited by every candidate model: small: HTTP 429"


def test_a_server_that_is_not_ollama_never_gets_ollama_advice(tmp_path, capsys, monkeypatch):
    other = {"base_url": "http://127.0.0.1:8080/v1", "local": True}
    raw = {**BASE, "providers": {**BASE["providers"], "local": other}}
    f = tmp_path / "config.yaml"
    f.write_text(json.dumps(raw), encoding="utf-8")
    from trioditis.__main__ import main

    def down(req):
        raise httpx.ConnectError("refused")

    _fake_network(monkeypatch, down)
    main(["--config", str(f), "check"])
    out = capsys.readouterr().out
    assert "server not answering: start the server" in out
    assert "local did not answer at http://127.0.0.1:8080/v1" in " ".join(out.split())
    assert "ollama" not in out.lower()
    r = make_client(down, cfg(providers=raw["providers"])).post("/v1/chat/completions", json=body("hello"))
    msg = r.json()["error"]["message"]
    assert "http://127.0.0.1:8080/v1" in msg and "start the server" in msg and "ollama" not in msg.lower()
    # a model missing from a listing it answered: no "ollama pull" either
    _fake_network(monkeypatch, lambda r: httpx.Response(200, json={"data": [{"id": "x"}]}))
    main(["--config", str(f), "check"])
    assert "ollama" not in capsys.readouterr().out.lower()


def test_the_suite_probe_is_offline_by_default(tmp_path, capsys):
    out = _run_check(tmp_path, capsys)  # no mock in this test: the autouse fixture answers "refused"
    assert "server not answering" in out
