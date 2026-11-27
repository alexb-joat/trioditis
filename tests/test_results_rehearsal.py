"""`trioditis results` end to end: the stand-in server, the rehearsal script, and the contract between the file
`measure.run` writes and the collector that reads shared files (`scripts/collect_results.py`).

The stand-in's numbers measure nothing; these tests check that the pieces fit, never how good a model is."""

from __future__ import annotations

import contextlib
import json
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import collect_results as cr  # noqa: E402
import fake_ollama  # noqa: E402
import rehearse_results  # noqa: E402

from tests.test_measure import EMBEDDINGS_DIGEST, PICKER_DIGEST, Server, run  # noqa: E402
from trioditis import __version__, measure  # noqa: E402
from trioditis.evalroutes import read_labels, request_text  # noqa: E402
from trioditis.router import RoutePickerClient  # noqa: E402

LINE = f"results rehearsal OK: trioditis {__version__} (source tree), 315 prompts, collector accepted"


@pytest.fixture
def stand_in():
    server = fake_ollama.make_server(0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join(5)


# ---------------------------------------------------------------- the writer-checker contract

def test_the_file_measure_run_writes_passes_the_collector(tmp_path):
    """The shape `measure.run` writes (here from the httpx mock, with Ollama-shaped digests) is the shape the
    collector accepts: the contract no longer rests on a hand-made fixture alone."""
    run(tmp_path, Server())
    row = cr.check(cr.parse((tmp_path / "trioditis-results.json").read_text(encoding="utf-8")))
    assert (row["picker"], row["server"], row["n"], row["router"], row["picker_digest_12"]) == (
        "tiny:1b", "ollama", 315, "router", PICKER_DIGEST[:12])
    assert EMBEDDINGS_DIGEST != PICKER_DIGEST


def test_a_file_without_the_embeddings_model_passes_the_collector_too(tmp_path):
    run(tmp_path, Server(embeddings=False))
    row = cr.check(cr.parse((tmp_path / "trioditis-results.json").read_text(encoding="utf-8")))
    assert row["router"] == "router_keywords" and row["n"] == 315


def test_a_file_with_own_labels_passes_the_collector_with_counts_only(tmp_path):
    labels = tmp_path / "mine.csv"
    labels.write_text("prompt,expected_route\n" + "".join(f"please write item {i},writing\n" for i in range(6))
                      + "".join(f"fix this python bug {i},code\n" for i in range(6)), encoding="utf-8")
    run(tmp_path, Server(), own_labels=str(labels))
    text = (tmp_path / "trioditis-results.json").read_text(encoding="utf-8")
    assert cr.check(cr.parse(text))["own_labels_n"] == 12 and "please write item" not in text


# ---------------------------------------------------------------- the stand-in server

def test_the_stand_in_answers_the_shapes_ollama_does(stand_in):
    base = f"http://127.0.0.1:{stand_in.server_address[1]}"
    with httpx.Client(trust_env=False, timeout=5) as c:
        assert c.get(f"{base}/api/version").json() == {"version": fake_ollama.VERSION}
        models = c.get(f"{base}/api/tags").json()["models"]
        assert [m["name"] for m in models] == ["tiny:1b", "nomic-embed-text:latest"]
        assert all(len(m["digest"]) == 64 and set(m["digest"]) <= set("0123456789abcdef") for m in models)
        chat = c.post(f"{base}/v1/chat/completions", json={"messages": [{"role": "system", "content": "s"},
                                                                         {"role": "user", "content": "hello"}]}).json()
        assert chat["choices"][0]["message"]["content"] in fake_ollama.ROUTES
        emb = c.post(f"{base}/v1/embeddings", json={"model": "x", "input": ["a b", "b c", "d"]}).json()["data"]
        assert [d["index"] for d in emb] == [0, 1, 2] and all(len(d["embedding"]) == fake_ollama.DIMS for d in emb)
        assert c.get(f"{base}/nothing").status_code == 404
        assert c.post(f"{base}/v1/embeddings", json={"input": 5}).status_code == 400
        assert c.post(f"{base}/v1/chat/completions", content=b"not json").status_code == 400


def test_the_stand_in_is_neither_all_right_nor_all_wrong_and_does_not_change_its_mind():
    cfg = measure.measured_config("tiny:1b", "http://127.0.0.1:1/v1")
    picker = RoutePickerClient(cfg, None)
    rows = read_labels(measure.heldout_path(), cfg.routes)
    asked = [picker.body(request_text(r.prompt))["messages"][-1]["content"] for r in rows]
    replies = [fake_ollama.chat_reply(t) for t in asked]
    assert replies == [fake_ollama.chat_reply(t) for t in asked]
    right = sum(r.expected == a for r, a in zip(rows, replies, strict=True))
    assert 0.05 * len(rows) < right < 0.60 * len(rows) and set(replies) == set(fake_ollama.ROUTES)


def test_the_stand_in_listens_on_loopback_only_and_needs_a_port(stand_in, capsys):
    assert stand_in.server_address[0] == "127.0.0.1"
    for argv in ([], ["--port", "8080", "--host", "0.0.0.0"], ["--port", "0"]):
        with pytest.raises(SystemExit) as e:
            fake_ollama.main(argv)
        assert e.value.code == 2
    assert "its numbers measure nothing" in (fake_ollama.__doc__ or "").lower().replace("\n", " ")
    capsys.readouterr()


# ---------------------------------------------------------------- the rehearsal script

def test_the_rehearsal_runs_the_real_command_from_the_source_tree():
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "rehearse_results.py"), "--python", sys.executable,
                        "--source-tree"], capture_output=True, text=True, timeout=300, cwd=REPO)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.strip() == LINE


def test_the_rehearsal_stops_the_stand_in_it_started(monkeypatch):
    port = rehearse_results.free_port()
    monkeypatch.setattr(rehearse_results, "free_port", lambda: port)
    assert rehearse_results.rehearse(sys.executable, "source tree") == LINE
    with pytest.raises(httpx.TransportError), httpx.Client(trust_env=False, timeout=2) as c:
        c.get(f"http://127.0.0.1:{port}/api/version")


@contextlib.contextmanager
def silent_child(log: Path, says: str):
    """A child that prints `says` (when given) and then never answers anything, the way a stuck stand-in looks."""
    with log.open("w", encoding="utf-8") as fh:
        proc = subprocess.Popen([sys.executable, "-c", f"import time; print({says!r} or '', end='', flush=True); "
                                 "time.sleep(60)"], stdout=fh, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 30
        while says and says not in log.read_text(encoding="utf-8") and time.monotonic() < deadline:
            time.sleep(0.05)
        yield proc
    finally:
        proc.kill()
        proc.wait()


def test_a_stand_in_that_never_answers_is_described_not_just_timed_out(tmp_path):
    """CI on macOS (26 Nov 2026) said only "did not answer ... within 15 s". The message now says whether the
    stand-in still runs, whether anything has bound its port, what the last try met and what the stand-in printed."""
    port = rehearse_results.free_port()
    url = f"http://127.0.0.1:{port}/api/version"
    with silent_child(tmp_path / "quiet.log", "") as proc:
        with pytest.raises(rehearse_results.RehearsalError) as e:
            rehearse_results.wait_until_up(url, proc, tmp_path / "quiet.log", wait_s=0.5)
        message = str(e.value)
    assert message.startswith(f"the stand-in server did not answer {url} within 0.5 s (")
    assert "tries, the last: " in message and "the last: none made" not in message
    assert f"the stand-in is still running (pid {proc.pid})" in message
    assert f"port {port} on 127.0.0.1 is free (nothing has bound it)" in message
    assert message.endswith("it printed nothing (it prints its address once it is listening)")


def test_a_bound_port_and_what_the_stand_in_printed_are_named(tmp_path):
    """A socket that has bound the port but does not listen: the case of a stand-in stuck between bind and listen."""
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        port = held.getsockname()[1]
        with silent_child(tmp_path / "said.log", "fake-ollama on its way") as proc:
            with pytest.raises(rehearse_results.RehearsalError) as e:
                rehearse_results.wait_until_up(f"http://127.0.0.1:{port}/api/version", proc, tmp_path / "said.log",
                                               wait_s=0.5)
    message = str(e.value)
    assert f"port {port} on 127.0.0.1 is taken (a socket has bound it)" in message
    assert message.endswith("it printed: fake-ollama on its way")


def test_a_stand_in_that_answers_404_is_named_by_its_status(tmp_path):
    server = fake_ollama.make_server(0, "no-api")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with silent_child(tmp_path / "s.log", "") as proc:
            with pytest.raises(rehearse_results.RehearsalError, match=r"the last: HTTP 404\)"):
                rehearse_results.wait_until_up(f"http://127.0.0.1:{server.server_address[1]}/api/version", proc,
                                               tmp_path / "s.log", wait_s=0.5)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def test_the_rehearsal_checks_the_answers_file_too(tmp_path):
    run(tmp_path, Server(), answers=str(tmp_path / "a.json"))
    doc = json.loads((tmp_path / "trioditis-results.json").read_text(encoding="utf-8"))
    answers = json.loads((tmp_path / "a.json").read_text(encoding="utf-8"))
    rehearse_results.check_answers(answers, doc)                                  # what the real command writes passes
    for mutate in (lambda a: a.update(extra=1), lambda a: a.update(heldout_sha256_16="0" * 16),
                   lambda a: a["answers"].pop(), lambda a: a["answers"][0].update(route="poetry"),
                   lambda a: a["answers"][0].update(row=99), lambda a: a["answers"][0].update(prompt="text")):
        bad = json.loads(json.dumps(answers))
        mutate(bad)
        with pytest.raises(rehearse_results.RehearsalError):
            rehearse_results.check_answers(bad, doc)


def test_the_child_gets_a_small_environment_and_dead_proxies(monkeypatch, tmp_path):
    monkeypatch.setenv("ZQX_SECRET_MARKER", "do-not-pass-on")
    env = rehearse_results.child_env(tmp_path)
    assert "ZQX_SECRET_MARKER" not in env and "do-not-pass-on" not in env.values()
    assert Path(env["TRIODITIS_HOME"]).is_relative_to(tmp_path) and Path(env["TRIODITIS_ENV_FILE"]).is_relative_to(tmp_path)
    proxies = {env[k] for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")}
    assert proxies == {rehearse_results.DEAD_PROXY} and "NO_PROXY" not in env


def test_installed_mode_refuses_code_that_comes_from_the_checkout(tmp_path):
    seen = subprocess.run([sys.executable, "-c", "import trioditis; print(trioditis.__file__)"], cwd=tmp_path,
                          capture_output=True, text=True, check=False)
    if seen.returncode == 0 and not Path(seen.stdout.strip()).resolve().is_relative_to(REPO):
        pytest.skip("a wheel of trioditis is installed in this environment")
    with pytest.raises(rehearse_results.RehearsalError) as e:
        rehearse_results.rehearse(sys.executable, "installed wheel")
    assert "trioditis" in str(e.value)


def test_a_failed_rehearsal_says_so_and_exits_1(capsys, tmp_path):
    code = rehearse_results.main(["--python", str(tmp_path / "no-such-python"), "--source-tree"])
    assert code == 1
    assert "results rehearsal FAILED" in capsys.readouterr().err


# ---------------------------------------------------------------- what a stranger meets, over real sockets

@contextlib.contextmanager
def serving(mode: str = "ok", after: int = 0):
    server = fake_ollama.make_server(0, mode, after)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def results_cli(tmp_path: Path, port: int, *extra: str, out: Path | None = None):
    """`python -m trioditis results` as a person runs it: its own process, its own environment, real sockets."""
    out = out or tmp_path / "r.json"
    proc = subprocess.run([sys.executable, "-m", "trioditis", "results", "--no-share", "--model", "tiny:1b",
                           "--base-url", f"http://127.0.0.1:{port}/v1", "--out", str(out), *extra],
                          cwd=REPO, env=rehearse_results.child_env(tmp_path), capture_output=True, text=True,
                          timeout=300, check=False)
    return proc, out


def clean_failure(proc, out: Path, *words: str) -> str:
    """Exit 1, no traceback, no results file, and a last line that starts `results:` and holds each word."""
    assert proc.returncode == 1, proc.stdout[-500:] + proc.stderr[-500:]
    assert "Traceback" not in proc.stderr + proc.stdout
    assert not out.exists()
    last = [ln for ln in proc.stderr.splitlines() if ln.strip()][-1]
    assert last.startswith("results: ")
    for word in words:
        assert word in last, (word, last)
    return last


def test_nothing_listening_says_to_start_the_server_or_pass_its_address(tmp_path):
    proc, out = results_cli(tmp_path, rehearse_results.free_port())
    clean_failure(proc, out, "no answer from", "ollama serve", "--base-url")


def test_a_server_error_for_the_picker_model_is_not_blamed_on_a_missing_pull(tmp_path):
    with serving("chat-500") as server:
        proc, out = results_cli(tmp_path, server.server_address[1])
    last = clean_failure(proc, out, "HTTP 500", "server's own log", "--model")
    assert "ollama pull" not in last


@pytest.mark.parametrize("mode", ["chat-not-json", "chat-wrong-shape"])
def test_a_200_that_is_not_a_chat_completion_stops_the_run_before_any_timing(tmp_path, mode):
    with serving(mode) as server:
        proc, out = results_cli(tmp_path, server.server_address[1])
        calls = server.request_count
    clean_failure(proc, out, "HTTP 200 but not with a chat completion", "OpenAI-style", "--base-url")
    assert calls == 3 and "timing the picker" not in proc.stderr       # version, tags, the first call: nothing more


def test_a_server_that_hangs_up_on_the_first_call_is_a_no_answer(tmp_path):
    with serving("close-after", after=2) as server:                      # /api/version and /api/tags, then silence
        proc, out = results_cli(tmp_path, server.server_address[1])
    clean_failure(proc, out, "no answer from", "ollama serve")


def test_a_server_that_drops_out_mid_run_is_not_a_measurement_and_is_not_offered_for_sharing(tmp_path):
    with serving("close-after", after=25) as server:                      # 3 setup calls, 15 timing calls, 7 prompts, then silence
        proc, out = results_cli(tmp_path, server.server_address[1])
    clean_failure(proc, out, "308 of 315 picker calls", "failed even after a retry", "Nothing was written",
                  "nothing is offered for sharing", "still running")


def test_without_the_embeddings_model_the_run_says_so_and_the_picker_still_counts(tmp_path):
    with serving("no-embeddings") as server:
        proc, out = results_cli(tmp_path, server.server_address[1])
    assert proc.returncode == 0 and "Traceback" not in proc.stderr
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["heldout"]["router"] is None and "ollama pull nomic-embed-text" in doc["heldout"]["router_note"]
    assert "not graded: nomic-embed-text did not answer" in proc.stdout and doc["heldout"]["picker"]["n"] == 315


def test_a_server_without_ollamas_api_still_runs_and_the_file_says_the_server_is_unknown(tmp_path):
    with serving("no-api") as server:
        proc, out = results_cli(tmp_path, server.server_address[1])
    assert proc.returncode == 0 and "Traceback" not in proc.stderr
    models = json.loads(out.read_text(encoding="utf-8"))["models"]
    assert models["server"] is None and models["picker_digest"] is None and models["embeddings_digest"] is None
    assert 'counted as an "unknown server" row' in proc.stdout
    assert cr.check(cr.parse(out.read_text(encoding="utf-8")))["server"] == "unknown server"


def test_an_out_folder_that_does_not_exist_is_found_before_the_first_call(tmp_path):
    with serving() as server:
        proc, out = results_cli(tmp_path, server.server_address[1], out=tmp_path / "no-such-folder" / "r.json")
        calls = server.request_count
    clean_failure(proc, out, "its folder does not exist", "--out", "create it or pass another")
    assert calls == 0


def test_an_out_that_is_a_folder_is_found_before_the_first_call(tmp_path):
    with serving() as server:
        proc, _ = results_cli(tmp_path, server.server_address[1], out=tmp_path)
        calls = server.request_count
    clean_failure(proc, tmp_path / "never-written.json", "is a folder", "file name")
    assert calls == 0


@pytest.mark.parametrize("name", ["missing.csv", ""])
def test_own_labels_that_are_not_a_file_name_the_right_flag(tmp_path, name):
    path = tmp_path / name                                               # "" is the folder itself
    with serving() as server:
        proc, out = results_cli(tmp_path, server.server_address[1], "--own-labels", str(path))
        calls = server.request_count
    last = clean_failure(proc, out, "--own-labels", "is not a file", "prompt and expected_route")
    assert "--labels FILE" not in last and calls == 0


def test_own_labels_with_other_route_names_say_which_routes_exist(tmp_path):
    labels = tmp_path / "mine.csv"
    labels.write_text("prompt,expected_route\nplease debug this,coding\nwrite a poem,poetry\n", encoding="utf-8")
    with serving() as server:
        proc, out = results_cli(tmp_path, server.server_address[1], "--own-labels", str(labels))
        calls = server.request_count
    last = clean_failure(proc, out, "'coding' is not a route", "code, reasoning, writing, quick", "shipped routes")
    assert "(did you mean 'code'?)" in last and calls == 0


def test_the_two_modes_are_exclusive_and_one_is_required():
    for argv in (["--python", sys.executable], ["--python", sys.executable, "--source-tree", "--installed"], ["--source-tree"]):
        with pytest.raises(SystemExit) as e:
            rehearse_results.main(argv)
        assert e.value.code == 2


def test_the_stand_ins_bind_without_a_reverse_dns_lookup(monkeypatch):
    """HTTPServer.server_bind looks up socket.getfqdn of its own address, which can stall for many seconds on
    GitHub's macOS runners. Both stand-ins skip it: with getfqdn refused they still bind, and fake_ollama answers."""
    import socket

    import console_check

    def refuse(*args, **kwargs):
        raise AssertionError("socket.getfqdn was called while binding")

    monkeypatch.setattr(socket, "getfqdn", refuse)
    with serving() as server:
        r = httpx.get(f"http://127.0.0.1:{server.server_address[1]}/api/version", trust_env=False, timeout=5)
    assert r.status_code == 200 and server.server_name == "127.0.0.1"
    stand_in = console_check.LocalServer(("127.0.0.1", 0), fake_ollama.Handler)
    try:
        assert stand_in.server_name == "127.0.0.1" and stand_in.server_port == stand_in.server_address[1] > 0
    finally:
        stand_in.server_close()
