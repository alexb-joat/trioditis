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


@pytest.mark.parametrize("answer", ["models", "refused", "unreachable"])
def test_check_sends_a_local_servers_key_and_never_prints_it(tmp_path, capsys, monkeypatch, answer):
    from trioditis.__main__ import main
    key = "local-MARKER-never-printed"
    sent: list[str | None] = []

    def handler(req):
        sent.append(req.headers.get("Authorization"))
        if answer == "unreachable":  # even an error whose text carries the key shows only its type
            raise httpx.ConnectError(f"cannot reach with {key}")
        if answer == "refused":
            return httpx.Response(401, text=f"bad key {key}")
        return httpx.Response(200, json={"data": [{"id": "llama3.2:3b"}]})

    _fake_network(monkeypatch, handler)
    monkeypatch.setenv("LOCAL_KEY", key)
    conf = json.loads(json.dumps(BASE))
    conf["providers"]["local"]["api_key_env"] = "LOCAL_KEY"
    f = tmp_path / "config.yaml"
    f.write_text(json.dumps(conf), encoding="utf-8")
    main(["--config", str(f), "check"])
    seen = capsys.readouterr()
    assert sent and set(sent) == {f"Bearer {key}"}  # the key goes to the local server, in the header
    assert "MARKER" not in seen.out + seen.err
    state = {"models": "ready", "refused": "server answers (HTTP 401); model not checked",
             "unreachable": "server not answering: start it (ollama serve)"}[answer]
    assert _rows(seen.out)["small"].endswith(state)


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


def test_the_readme_shows_what_check_really_prints(capsys, monkeypatch):
    """The Quick start prints `check` output for the models its step 1 pulls and no cloud keys."""
    from pathlib import Path

    from trioditis.__main__ import main
    repo = Path(__file__).resolve().parents[1]
    _fake_network(monkeypatch, lambda r: httpx.Response(200, json={"data": [
        {"id": "llama3.2:3b"}, {"id": "qwen2.5-coder:7b"}, {"id": "nomic-embed-text:latest"}]}))
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    main(["--config", str(repo / "config.example.yaml"), "check"])
    out = capsys.readouterr().out.strip()
    assert out.startswith("config OK") and out in (repo / "README.md").read_text(encoding="utf-8")



@pytest.mark.parametrize("cmd", ["check", "serve", "explain"])
def test_a_missing_config_says_how_to_set_one_up(tmp_path, monkeypatch, cmd):
    # The first thing a new user may run: `serve` outside the folder `init` made.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TRIODITIS_CONFIG", raising=False)
    from trioditis.__main__ import main
    argv = [cmd, "hello"] if cmd == "explain" else [cmd]
    with pytest.raises(SystemExit) as e:
        main(argv)
    msg = str(e.value.code)
    assert f"no config at {tmp_path / 'config.yaml'}" in msg
    assert "trioditis init --dir" in msg and "--config" in msg


@pytest.mark.parametrize("listed,expect", [
    (["llama3.2:3b", "nomic-embed-text:latest"], "embeddings: nomic-embed-text on local: ready"),
    (["llama3.2:3b"], "not on this server: ollama pull nomic-embed-text; until then, routes use keywords"),
])
def test_check_reports_the_embeddings_model(tmp_path, capsys, monkeypatch, listed, expect):
    # A missing embeddings model only showed in each decision's reasons; `check` now says it, and what it costs.
    from trioditis.__main__ import main
    asked = _fake_network(monkeypatch, lambda r: httpx.Response(200, json={"data": [{"id": i} for i in listed]}))
    f = tmp_path / "config.yaml"
    f.write_text(json.dumps({**BASE, "embeddings": {"provider": "local", "model": "nomic-embed-text"}}),
                 encoding="utf-8")
    main(["--config", str(f), "check"])
    out = " ".join(capsys.readouterr().out.split())  # textwrap may break the long line
    assert expect in out
    assert all(u.startswith("http://localhost:11434/") for u in asked)  # local only, never the cloud
    if "ready" not in expect:
        assert "116 of 315" in out


def test_check_says_when_embeddings_are_off(tmp_path, capsys, monkeypatch):
    _fake_network(monkeypatch, lambda r: httpx.Response(200, json={"data": [{"id": "llama3.2:3b"}]}))
    out = " ".join(_run_check(tmp_path, capsys).split())
    assert "embeddings: off; routes use keywords and patterns alone" in out


def test_check_warns_when_a_local_provider_has_a_public_address():
    from trioditis.localcheck import local_scope_lines
    names = {"gpu.example.com": "93.184.216.34", "box.lan": "192.168.1.20", "pc.tail1234.ts.net": "100.101.102.103"}
    asked = []

    def resolve(host, port):
        asked.append(host)
        if host not in names:
            raise OSError("no such name")
        return [(2, 1, 6, "", (names[host], 0))]

    providers = {name: {"base_url": url, "local": True} for name, url in {
        "local": "http://localhost:11434/v1", "lan": "https://box.lan/v1", "tail": "https://pc.tail1234.ts.net/v1",
        "rented": "https://gpu.example.com/v1", "literal": "https://8.8.8.8/v1", "private": "https://10.0.0.5/v1",
        "gone": "https://nowhere.invalid/v1"}.items()}
    providers["cloud"] = {"base_url": "https://api.example.com/v1", "api_key_env": "CLOUD_KEY"}  # not local: not asked
    lines = local_scope_lines(cfg(providers=providers), resolve=resolve)
    assert [ln.split(" is marked")[0] for ln in lines] == ["rented", "literal"]
    assert "gpu.example.com is a public address (93.184.216.34)" in lines[0]
    assert "api.example.com" not in asked and "localhost" not in asked  # cloud and known local names: no lookup
