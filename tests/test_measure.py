"""`trioditis results` (trioditis/measure.py): what it measures, what the file may hold, and that it shares nothing
unless the person says yes."""

from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import collect_results as cr  # noqa: E402

from trioditis import measure  # noqa: E402
from trioditis.hardware import GPU, Hardware  # noqa: E402

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

    def __init__(self, *, chat_status: int = 200, embeddings: bool = True, reply=None, fail_chat=None, api: bool = True):
        self.requests: list[httpx.Request] = []
        self.chat_status, self.embeddings, self.api = chat_status, embeddings, api   # api=False: not Ollama, no /api/*
        self.reply = reply or (lambda text: "writing" if "write" in text.lower() else "code")
        self.fail_chat, self.chat_calls = fail_chat, 0   # fail_chat(n): the n-th chat call is dropped (n starts at 1)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.startswith("/api/") and not self.api:
            return httpx.Response(404, json={"error": "not found"})
        if path == "/api/version":
            return httpx.Response(200, json={"version": "0.13.0"})
        if path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "tiny:1b", "digest": PICKER_DIGEST},
                                                        {"name": "nomic-embed-text:latest", "digest": EMBEDDINGS_DIGEST}]})
        if path == "/v1/chat/completions":
            self.chat_calls += 1
            if self.fail_chat and self.fail_chat(self.chat_calls):
                raise httpx.ReadError("the server dropped the call", request=request)
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
    base = {"model": "tiny:1b", "base_url": "http://127.0.0.1:11434/v1", "own_labels": None, "answers": None,
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


def own_labels_file(folder, n: int = 12):
    labels = folder / "mine.csv"
    with open(labels, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["prompt", "expected_route"])
        for i, route in enumerate(["code", "writing", "quick", "reasoning"] * (n // 4)):
            w.writerow([f"{MARKER} please write item {i} for {HOST}", route])
    return labels


def test_the_answers_file_holds_the_rows_and_the_routes_named_and_nothing_else(tmp_path):
    answers = tmp_path / "a.json"
    run(tmp_path, Server(), answers=str(answers))
    doc = json.loads(answers.read_text(encoding="utf-8"))
    results = json.loads((tmp_path / "trioditis-results.json").read_text(encoding="utf-8"))
    assert set(doc) == {"schema", "trioditis", "heldout_sha256_16", "picker", "answers"}
    assert doc["schema"] == "trioditis-answers.v1" and doc["picker"] == "tiny:1b"
    assert doc["trioditis"] == results["trioditis"] and doc["heldout_sha256_16"] == results["settings"]["heldout_sha256_16"]
    assert all(set(a) == {"row", "route"} for a in doc["answers"])
    assert [a["row"] for a in doc["answers"]] == list(range(2, 317))          # the header is row 1; 315 prompts follow
    assert {a["route"] for a in doc["answers"]} <= {*results["settings"]["routes"], measure.NO_ROUTE}
    held = list(csv.DictReader(open(measure.heldout_path(), encoding="utf-8")))
    right = sum(a["route"] == h["expected_route"] for a, h in zip(doc["answers"], held, strict=True))
    assert right == results["heldout"]["picker"]["right"]                      # the same answers the counts came from


def test_a_reply_with_no_route_is_a_dash_in_the_answers(tmp_path):
    answers = tmp_path / "a.json"
    run(tmp_path, Server(reply=lambda text: "I am not sure"), answers=str(answers))
    assert {a["route"] for a in json.loads(answers.read_text(encoding="utf-8"))["answers"]} == {"-"}


def test_the_answers_file_holds_no_prompt_path_host_or_own_row_and_never_the_own_labels(tmp_path, capsys):
    """The marker-string test of the results file, on the answers file: own labels, their folder and the hardware
    notes must not reach it, and it has the 315 public rows whether or not own labels were given."""
    folder = tmp_path / f"home-{MARKER}-folder"
    folder.mkdir()
    labels = own_labels_file(folder)
    answers = folder / "answers.json"
    run(tmp_path, Server(), own_labels=str(labels), out=str(folder / "r.json"), answers=str(answers))
    text = answers.read_text(encoding="utf-8")
    printed = capsys.readouterr()
    for secret in (MARKER, HOST, str(tmp_path), "mine.csv", "please write item", "answers.json", "r.json"):
        assert secret not in text
    for prompt_text in ("please write item", HOST):      # the terminal names the files it wrote, never a prompt
        assert prompt_text not in printed.out and prompt_text not in printed.err
    held = list(csv.DictReader(open(measure.heldout_path(), encoding="utf-8")))
    assert not any(r["prompt"] in text for r in held)
    assert not re.search(r"\d\d:\d\d|\d{4}-\d\d-\d\d", text)                    # no time of day, no date
    assert len(json.loads(text)["answers"]) == 315                                # the 12 own rows are not in it
    assert json.loads((folder / "r.json").read_text(encoding="utf-8"))["own_labels"]["n"] == 12


def test_no_answers_file_is_written_unless_asked_for(tmp_path):
    run(tmp_path, Server())
    assert sorted(p.name for p in tmp_path.iterdir()) == ["trioditis-results.json"]


def test_a_failed_run_writes_no_answers_file(tmp_path):
    answers = tmp_path / "a.json"
    with pytest.raises(SystemExit):
        run(tmp_path, Server(fail_chat=lambda n: n > 100), answers=str(answers))
    assert not answers.exists() and not (tmp_path / "trioditis-results.json").exists()


@pytest.mark.parametrize("where, words", [("folder", "is a folder"), ("missing", "its folder does not exist"),
                                          ("same", "name the same file")])
def test_a_bad_answers_path_is_found_before_the_first_call(tmp_path, where, words):
    target = {"folder": tmp_path, "missing": tmp_path / "no-such" / "a.json", "same": tmp_path / "trioditis-results.json"}[where]
    server = Server()
    with pytest.raises(SystemExit) as e:
        run(tmp_path, server, answers=str(target))
    assert "--answers" in str(e.value) and words in str(e.value) and server.requests == []


def test_an_answers_file_that_cannot_be_written_says_the_results_file_was_written(tmp_path, monkeypatch):
    real = measure.Path.write_text

    def refuse_answers(self, *a, **k):
        if self.name == "a.json":
            raise PermissionError(13, "Permission denied")
        return real(self, *a, **k)
    monkeypatch.setattr(measure.Path, "write_text", refuse_answers)
    with pytest.raises(SystemExit) as e:
        run(tmp_path, Server(), answers=str(tmp_path / "a.json"))
    assert "cannot write --answers" in str(e.value) and "results file was written" in str(e.value)
    assert (tmp_path / "trioditis-results.json").exists()


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


def test_a_server_that_is_not_ollama_is_counted_as_unknown_server_and_the_run_says_so(tmp_path, capsys):
    run(tmp_path, Server(api=False))
    text = (tmp_path / "trioditis-results.json").read_text(encoding="utf-8")
    models = json.loads(text)["models"]
    assert models["server"] is None and models["server_version"] is None
    assert models["picker_digest"] is None and models["embeddings_digest"] is None
    out = capsys.readouterr().out
    assert 'counted as an "unknown server" row, kept apart from the Ollama results' in out
    assert cr.check(cr.parse(text))["server"] == "unknown server"            # the collector takes it, labelled


def test_an_ollama_run_does_not_mention_an_unknown_server(tmp_path, capsys):
    run(tmp_path, Server())
    assert "unknown server" not in capsys.readouterr().out


def test_a_reply_with_no_route_counts_as_wrong(tmp_path):
    run(tmp_path, Server(reply=lambda text: "I am not sure"))
    p = json.loads((tmp_path / "trioditis-results.json").read_text(encoding="utf-8"))["heldout"]["picker"]
    assert p["right"] == 0 and p["no_route"] == 315


def test_picker_calls_that_mostly_fail_make_no_file_and_no_offer_to_share(tmp_path, capsys):
    """A server that drops out leaves 'failed, counted as wrong' calls: not a measurement of the model."""
    server = Server(fail_chat=lambda n: n > 100)          # 1 first call + 15 timing calls + 84 prompts, then nothing
    with pytest.raises(SystemExit) as e:
        run(tmp_path, server, answer="y", tty=True)
    assert "of 315 picker calls on the held-out prompts failed even after a retry" in str(e.value)
    assert "Nothing was written" in str(e.value) and "nothing is offered for sharing" in str(e.value)
    assert not (tmp_path / "trioditis-results.json").exists()
    assert "[y/N]" not in capsys.readouterr().out


def test_exactly_half_failed_is_still_a_run_and_one_more_is_not(tmp_path):
    setup = 1 + 15                                         # the first call and the 15 timing calls come first
    half = 315 // 2                                        # 157 prompts fail on both tries: 2 x 157 < 315 is not "more than half"
    run(tmp_path, Server(fail_chat=lambda n: setup < n <= setup + 2 * half))
    doc = json.loads((tmp_path / "trioditis-results.json").read_text(encoding="utf-8"))
    assert doc["heldout"]["picker"]["failed_after_retry"] == half
    with pytest.raises(SystemExit):                        # 158 of 315 is more than half
        run(tmp_path, Server(fail_chat=lambda n: setup < n <= setup + 2 * (half + 1)))


def test_a_few_failed_calls_are_said_plainly_and_the_file_still_passes_the_collector(tmp_path, capsys):
    run(tmp_path, Server(fail_chat=lambda n: n in (100, 101)))   # one prompt fails, and so does its retry
    out = capsys.readouterr().out
    assert "Note: 1 of 315 picker calls on the held-out set failed even after a retry and count as wrong" in out
    text = (tmp_path / "trioditis-results.json").read_text(encoding="utf-8")
    assert json.loads(text)["heldout"]["picker"]["failed_after_retry"] == 1
    assert cr.check(cr.parse(text))["picker_failed"] == 1


def test_timing_calls_that_failed_are_said_too(tmp_path, capsys):
    run(tmp_path, Server(fail_chat=lambda n: n == 5))        # a timing call: no retry, left out of the times
    assert "1 of 15 timing calls failed and are left out of the times" in capsys.readouterr().out


def test_a_run_with_no_failed_call_prints_no_note(tmp_path, capsys):
    run(tmp_path, Server())
    assert "Note:" not in capsys.readouterr().out


def test_the_numbers_are_printed_before_the_error_when_the_file_cannot_be_written(tmp_path, monkeypatch, capsys):
    def refuse(self, *a, **k):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(measure.Path, "write_text", refuse)
    with pytest.raises(SystemExit) as e:
        run(tmp_path, Server())
    printed = capsys.readouterr().out
    assert json.loads(printed.split("\nHeld-out set")[0])["schema"] == "trioditis-results.v1"   # the whole file, on screen
    assert "cannot write --out" in str(e.value) and "Permission denied" in str(e.value) and "printed above" in str(e.value)


@pytest.mark.parametrize("body, ok", [({"choices": [{"message": {"content": "code"}}]}, True),
                                      ({"choices": [{"message": {"content": None}}]}, True),
                                      ({"choices": []}, False), ({"object": "list"}, False), ([], False),
                                      ({"choices": [{"text": "code"}]}, False)])
def test_what_counts_as_a_chat_completion(body, ok):
    assert measure._is_chat_completion(httpx.Response(200, json=body)) is ok
    assert measure._is_chat_completion(httpx.Response(200, text="<html>no</html>")) is False


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
    assert (a.model, a.base_url, a.out, a.no_share, a.own_labels, a.answers) == (
        "tiny:1b", "http://localhost:11434/v1", "trioditis-results.json", True, None, None)


def test_the_held_out_set_ships_in_the_package():
    rows = list(csv.DictReader(open(measure.heldout_path(), encoding="utf-8")))
    assert len(rows) == 315 and set(rows[0]) >= {"prompt", "expected_route"}
