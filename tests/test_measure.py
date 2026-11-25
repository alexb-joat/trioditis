"""`trioditis results` (trioditis/measure.py): what it measures, what the file may hold, and that it shares nothing
unless the person says yes."""

from __future__ import annotations

import csv
import json
import re
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from trioditis import measure
from trioditis.hardware import GPU, Hardware

MARKER = "ZQX-MARKER-7731"
HOST = "workstation-of-someone"
PICKER_DIGEST = "a1" * 32          # Ollama's /api/tags lists each digest as 64 lowercase hex characters
EMBEDDINGS_DIGEST = "b2" * 32


def fake_hardware() -> Hardware:
    return Hardware(os="Linux", arch="x86_64", cpu="Test CPU 8-core", cores=8, ram_total=16_000_000_000,
                    ram_available=9_000_000_000, gpus=[GPU("Test GPU", "nvidia", 8_000_000_000)],
                    notes=[f"probe on {HOST} failed"])


class Server:
    """A local Ollama stand-in that remembers every request it got."""

    def __init__(self, *, chat_status: int = 200, embeddings: bool = True, reply=None):
        self.requests: list[httpx.Request] = []
        self.chat_status, self.embeddings = chat_status, embeddings
        self.reply = reply or (lambda text: "writing" if "write" in text.lower() else "code")

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/api/version":
            return httpx.Response(200, json={"version": "0.13.0"})
        if path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "tiny:1b", "digest": PICKER_DIGEST},
                                                        {"name": "nomic-embed-text:latest", "digest": EMBEDDINGS_DIGEST}]})
        if path == "/v1/chat/completions":
            if self.chat_status != 200:
                return httpx.Response(self.chat_status, json={"error": "model not found"})
            text = json.loads(request.content)["messages"][1]["content"]
            return httpx.Response(200, json={"choices": [{"message": {"content": self.reply(text)}}]})
        if path == "/v1/embeddings" and self.embeddings:
            texts = json.loads(request.content)["input"]
            return httpx.Response(200, json={"data": [{"index": i, "embedding": [1.0, (len(t) % 7) / 7]}
                                                      for i, t in enumerate(texts)]})
        return httpx.Response(404, json={"error": "not found"})


def args(tmp_path, **kw) -> SimpleNamespace:
    base = {"model": "tiny:1b", "base_url": "http://127.0.0.1:11434/v1", "own_labels": None,
            "out": str(tmp_path / "trioditis-results.json"), "no_share": False}
    return SimpleNamespace(**{**base, **kw})


def run(tmp_path, server: Server, *, answer: str = "", tty: bool = False, **kw):
    opened: list[str] = []
    asked: list[str] = []

    def ask(q):
        asked.append(q)
        return answer

    code = measure.run(args(tmp_path, **kw), transport=httpx.MockTransport(server), hardware=fake_hardware,
                       isatty=lambda: tty, ask=ask, opener=lambda u: opened.append(u) or True, today=lambda: "2026-11-24")
    return code, opened, asked


def test_the_file_follows_the_schema_and_is_printed_in_full(tmp_path, capsys):
    code, _, asked = run(tmp_path, Server())
    assert code == 0 and asked == []                     # not a terminal: never asked
    out = capsys.readouterr().out
    text = (tmp_path / "trioditis-results.json").read_text(encoding="utf-8")
    assert out.startswith(text)
    doc = json.loads(text)
    assert doc["schema"] == "trioditis-results.v1" and doc["date"] == "2026-11-24"
    assert set(doc) == {"schema", "trioditis", "date", "settings", "models", "machine", "timing", "heldout", "own_labels"}
    assert doc["models"] == {"server": "ollama", "server_version": "0.13.0", "picker": "tiny:1b",
                             "picker_digest": PICKER_DIGEST, "embeddings": "nomic-embed-text",
                             "embeddings_digest": EMBEDDINGS_DIGEST}
    h = doc["heldout"]
    assert h["n"] == 315 and h["picker"]["n"] == 315 and h["router"]["n"] == 315 and h["router_note"] is None
    assert sum(r["n"] for r in h["picker"]["per_route"].values()) == 315
    assert h["picker"]["right"] == sum(r["right"] for r in h["picker"]["per_route"].values())
    assert 0 <= h["picker"]["low95"] <= h["picker"]["right"] / 315 <= h["picker"]["high95"] <= 1
    assert h["picker_vs_router"]["router"] == "router"
    assert [t["chars"] for t in doc["timing"]["by_chars"]] == list(measure.TIMING_LENGTHS)
    assert all(t["n"] == measure.TIMING_REPEATS for t in doc["timing"]["by_chars"])
    assert doc["settings"]["embeddings_threshold"] == 0.45 and doc["own_labels"] is None
    assert "Held-out set, 315 prompts" in out


def test_every_request_goes_to_the_local_server_and_none_through_a_proxy(tmp_path, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example:3128")
    server = Server()
    run(tmp_path, server)
    assert server.requests and {r.url.host for r in server.requests} == {"127.0.0.1"}


def test_the_file_holds_no_prompt_path_host_or_time_of_day(tmp_path, capsys):
    """Marker strings in a made-up label file, its folder name and the hardware notes must not reach the file."""
    folder = tmp_path / f"home-{MARKER}-folder"
    folder.mkdir()
    labels = folder / "mine.csv"
    with open(labels, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["prompt", "expected_route"])
        for i, route in enumerate(["code", "writing", "quick", "reasoning"] * 3):
            w.writerow([f"{MARKER} please write item {i} for {HOST}", route])
    run(tmp_path, Server(), own_labels=str(labels), out=str(folder / "r.json"))
    text = (folder / "r.json").read_text(encoding="utf-8")
    printed = capsys.readouterr()
    for secret in (MARKER, HOST, str(tmp_path), "mine.csv", "please write item"):
        assert secret not in text
    for prompt_text in ("please write item", HOST):      # the terminal names the file it wrote, never a prompt
        assert prompt_text not in printed.out and prompt_text not in printed.err
    heldout = list(csv.DictReader(open(measure.heldout_path(), encoding="utf-8")))
    assert not any(r["prompt"] in text for r in heldout)
    assert not re.search(r"\d\d:\d\d", text)            # the date only, no time of day
    own = json.loads(text)["own_labels"]
    assert own["n"] == 12 and own["router"]["n"] == 12 and set(own) >= {"picker", "router", "picker_vs_router"}


def test_a_server_that_is_not_on_this_machine_is_refused_before_any_call(tmp_path):
    server = Server()
    with pytest.raises(SystemExit) as e:
        run(tmp_path, server, base_url="http://192.168.1.20:11434/v1")
    assert "is not on this machine" in str(e.value) and server.requests == []
    assert not (tmp_path / "trioditis-results.json").exists()


def test_a_missing_model_says_how_to_pull_it(tmp_path):
    with pytest.raises(SystemExit) as e:
        run(tmp_path, Server(chat_status=404))
    assert "ollama pull tiny:1b" in str(e.value)
    assert not (tmp_path / "trioditis-results.json").exists()


def test_without_the_embeddings_model_the_router_row_says_why_and_the_picker_still_counts(tmp_path):
    run(tmp_path, Server(embeddings=False))
    h = json.loads((tmp_path / "trioditis-results.json").read_text(encoding="utf-8"))["heldout"]
    assert h["router"] is None and "ollama pull nomic-embed-text" in h["router_note"]
    assert h["picker"]["n"] == 315 and h["picker_vs_router"]["router"] == "router_keywords"


def test_a_reply_with_no_route_counts_as_wrong(tmp_path):
    run(tmp_path, Server(reply=lambda text: "I am not sure"))
    p = json.loads((tmp_path / "trioditis-results.json").read_text(encoding="utf-8"))["heldout"]["picker"]
    assert p["right"] == 0 and p["no_route"] == 315


@pytest.mark.parametrize("answer", ["", "n", "no", "  ", "maybe"])
def test_the_default_answer_shares_nothing(tmp_path, capsys, answer):
    _, opened, asked = run(tmp_path, Server(), answer=answer, tty=True)
    assert len(asked) == 1 and "[y/N]" in asked[0] and opened == []
    assert "Not shared. Nothing was sent anywhere." in capsys.readouterr().out


def test_no_share_never_asks(tmp_path):
    _, opened, asked = run(tmp_path, Server(), answer="y", tty=True, no_share=True)
    assert asked == [] and opened == []


def test_yes_opens_the_filled_in_form_and_sends_nothing_itself(tmp_path, capsys):
    server = Server()
    _, opened, _ = run(tmp_path, server, answer="y", tty=True)
    assert {r.url.host for r in server.requests} == {"127.0.0.1"}   # the form is the browser's business
    [url] = opened
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == measure.FORM_URL
    q = parse_qs(parts.query)
    assert q["template"] == ["results.yml"] and q["title"][0].startswith("Results: tiny:1b on Linux")
    assert json.loads(q["results"][0]) == json.loads((tmp_path / "trioditis-results.json").read_text(encoding="utf-8"))
    assert "press Submit" in capsys.readouterr().out


def test_a_link_too_long_asks_for_a_paste_instead(capsys):
    doc = {"models": {"picker": "m"}, "machine": {"os": "Linux", "arch": "x86_64"}, "pad": "x" * measure.MAX_URL}
    assert measure.form_url(doc) is None
    opened: list[str] = []
    assert measure.ask_to_share(doc, ask=lambda q: "yes", opener=opened.append) is False
    assert opened == [] and "paste the contents of trioditis-results.json" in capsys.readouterr().out


def test_a_short_link_fits():
    doc = {"models": {"picker": "m"}, "machine": {"os": "Linux", "arch": "x86_64"}, "n": 1}
    url = measure.form_url(doc)
    assert url and len(url) <= measure.MAX_URL and "template=results.yml" in url


def test_the_command_line_reaches_the_command_without_a_config(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.chdir(tmp_path)                           # no config.yaml here
    monkeypatch.setattr(measure, "run", lambda a: seen.setdefault("args", a) and 0)
    from trioditis.__main__ import main
    with pytest.raises(SystemExit) as e:
        main(["results", "--model", "tiny:1b", "--no-share"])
    assert e.value.code == 0
    a = seen["args"]
    assert (a.model, a.base_url, a.out, a.no_share, a.own_labels) == ("tiny:1b", "http://localhost:11434/v1",
                                                                      "trioditis-results.json", True, None)


def test_the_held_out_set_ships_in_the_package():
    rows = list(csv.DictReader(open(measure.heldout_path(), encoding="utf-8")))
    assert len(rows) == 315 and set(rows[0]) >= {"prompt", "expected_route"}
