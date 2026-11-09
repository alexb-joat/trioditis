import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import effort_trial as t  # noqa: E402


def test_command_is_plain_chat_on_the_subscription():
    cmd = t.claude_cmd("claude-opus-5-5", "max")
    assert cmd[:2] == ["claude", "-p"] and "--bare" not in cmd  # --bare means API-key billing
    assert cmd[cmd.index("--effort") + 1] == "max" and cmd[cmd.index("--tools") + 1] == ""
    assert cmd[cmd.index("--model") + 1] == "claude-opus-5-5"
    with pytest.raises(ValueError):
        t.claude_cmd("claude-opus-5-5", "ultra")


WORD = {"high": "alpha", "max": "omega"}


def _fake_runner(out_by_effort):
    def run(cmd, **kw):
        effort = cmd[cmd.index("--effort") + 1]
        doc = {"session_id": "s-" + effort, "is_error": False, "result": "reply " + WORD[effort],
               "usage": {"input_tokens": 5, "output_tokens": out_by_effort[effort] * len(kw["input"]),
                         "output_tokens_details": {"thinking_tokens": 1}}}
        return subprocess.CompletedProcess(cmd, 0, json.dumps(doc), "")
    return run


def _meter(path, weekly):
    path.write_text(json.dumps({"history": [{"at": 1_800_000_000 + i, "limits": [
        {"kind": "weekly_all", "percent": w}, {"kind": "session", "percent": w * 2}]} for i, w in enumerate(weekly)]}))


def _args(tmp_path, **kw):
    prompts = tmp_path / "prompts.json"
    prompts.write_text(json.dumps({"prompts": [{"id": "a", "text": "x"}, {"id": "b", "text": "xx"}]}))
    import argparse
    base = dict(prompts=str(prompts), out=str(tmp_path / "out"), efforts="high,max", model="m",
                meter_file=str(tmp_path / "meter.json"), gap=0, max_weekly_rise=3.0, timeout=5, dry_run=False)
    base.update(kw)
    return argparse.Namespace(**base)


def test_run_report_and_blind(tmp_path):
    _meter(tmp_path / "meter.json", [10])
    a = _args(tmp_path)
    assert t.cmd_run(a, runner=_fake_runner({"high": 100, "max": 300}), sleep=lambda s: None) == 0
    out = Path(a.out)
    s = t.summarise(t.load_runs(out), json.loads((out / "batches.json").read_text()))
    assert s["output_tokens"] == {"a": {"high": 100, "max": 300}, "b": {"high": 200, "max": 600}}
    assert s["ratios"]["max/high"]["ratio"] == pytest.approx(3.0) and s["ratios"]["max/high"]["n"] == 2
    assert (out / "runs" / "max-a.json").stat().st_mode & 0o077 == 0  # answers are private
    import argparse
    assert t.cmd_blind(argparse.Namespace(out=a.out, prompts=a.prompts, seed=1)) == 0
    blind = (out / "blind.json").read_text()
    assert "max" not in blind and "high" not in blind and "300" not in blind  # nothing gives the effort away
    key = json.loads((out / "key.json").read_text())
    items = json.loads(blind)["items"]
    for it in items:
        for ans in it["answers"]:
            assert ans["text"] == "reply " + WORD[key[it["id"]][ans["letter"]]]


def test_the_weekly_guard_stops_before_the_next_batch(tmp_path):
    meter = tmp_path / "meter.json"
    _meter(meter, [10])
    a = _args(tmp_path)
    calls = []

    def runner(cmd, **kw):
        calls.append(cmd[cmd.index("--effort") + 1])
        _meter(meter, [10, 14])  # the first batch pushed the weekly meter 4 points
        return _fake_runner({"high": 1, "max": 1})(cmd, **kw)
    assert t.cmd_run(a, runner=runner, sleep=lambda s: None) == 3
    assert set(calls) == {"high"}


def test_a_failed_run_is_kept_as_an_error_and_redone(tmp_path):
    _meter(tmp_path / "meter.json", [10])
    a = _args(tmp_path, efforts="high")

    def boom(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 5)
    t.cmd_run(a, runner=boom, sleep=lambda s: None)
    assert "timed out" in json.loads((Path(a.out) / "runs" / "high-a.json").read_text())["error"]
    t.cmd_run(a, runner=_fake_runner({"high": 7}), sleep=lambda s: None)
    assert json.loads((Path(a.out) / "runs" / "high-a.json").read_text())["error"] is None


def test_ledger_check_matches_runs_by_salted_session_and_model(tmp_path):
    from trioditis.plan import _hash
    from trioditis_metrics.db import MetricsDB
    from trioditis_metrics.plan import merge_events
    db = MetricsDB(tmp_path / "m.db")
    fp = lambda sid: _hash("salt", "session\0" + sid, 16)  # noqa: E731
    merge_events(db.conn, [
        {"id": "1", "at": 1.0, "source": "claude-code", "tokens_measured": 1, "agent": 0, "model": "m", "tokens_out": 300, "thread_fp": fp("s-max")},
        {"id": "2", "at": 1.0, "source": "claude-code", "tokens_measured": 1, "agent": 0, "model": "haiku", "tokens_out": 7, "thread_fp": fp("s-max")},  # side call: left out
        {"id": "3", "at": 1.0, "source": "claude-code", "tokens_measured": 1, "agent": 0, "model": "m", "tokens_out": 90, "thread_fp": fp("s-high")}])
    run = lambda e, sid, out: {"prompt": "a", "effort": e, "model": "m", "session_id": sid,  # noqa: E731
                               "error": None, "usage": {"output_tokens": out}}
    res = t.ledger_check([run("max", "s-max", 300), run("high", "s-high", 100), run("xhigh", "s-x", 5),
                          {**run("low", "s-low", 1), "error": "timed out"}], db.conn, "salt")
    assert res["matched"] == 1 and res["missing"] == ["xhigh-a"]
    assert res["differ"] == [{"run": "high-a", "run_out": 100, "ledger_out": 90}]
    db.close()


def test_a_meter_reading_older_than_the_last_batch_is_flagged():
    b = lambda e, start, end, at: {"effort": e, "start": start, "end": end,  # noqa: E731
                                   "meter_before": {"session": {"at": at, "percent": 1.0}}}
    s = t.summarise([], [b("high", 100, 200, 90), b("xhigh", 300, 400, 150), b("max", 500, 600, 450)])
    assert [m["stale"] for m in s["meter"]] == [False, True, False]
    assert s["meter"][1]["reading_age_min"] == 2.5
