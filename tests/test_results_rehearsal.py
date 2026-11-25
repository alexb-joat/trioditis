"""`trioditis results` end to end: the stand-in server, the rehearsal script, and the contract between the file
`measure.run` writes and the collector that reads shared files (`scripts/collect_results.py`).

The stand-in's numbers measure nothing; these tests check that the pieces fit, never how good a model is."""

from __future__ import annotations

import subprocess
import sys
import threading
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


def test_the_child_gets_a_small_environment_and_dead_proxies(monkeypatch, tmp_path):
    monkeypatch.setenv("ZQX_SECRET_MARKER", "do-not-pass-on")
    env = rehearse_results.child_env(tmp_path)
    assert "ZQX_SECRET_MARKER" not in env and "do-not-pass-on" not in env.values()
    assert Path(env["HLIDSKJALV_HOME"]).is_relative_to(tmp_path) and Path(env["TRIODITIS_ENV_FILE"]).is_relative_to(tmp_path)
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


def test_the_two_modes_are_exclusive_and_one_is_required():
    for argv in (["--python", sys.executable], ["--python", sys.executable, "--source-tree", "--installed"], ["--source-tree"]):
        with pytest.raises(SystemExit) as e:
            rehearse_results.main(argv)
        assert e.value.code == 2
