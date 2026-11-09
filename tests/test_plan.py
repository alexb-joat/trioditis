"""Plan usage: Claude Code transcripts, plan-usage.v1 files and meter readings."""

import json
import os
import sqlite3

import pytest

from trioditis.__main__ import main
from trioditis.config import ConfigError, load_plan_settings, parse_plan
from trioditis.plan import clean_event, read_meter_file, transcript_events
from trioditis_metrics.db import MetricsDB, usage
from trioditis_metrics.plan import meter_rise, plan_usage

MARKERS = ["SECRET-PROMPT-TEXT", "acme-client-folder", "feature/acme-branch", "Acme title", "/home/someone/acme"]


def _line(mid, out, *, model="claude-opus-5-5", effort="xhigh", side=False, ts="2026-11-08T10:00:00.000Z",
          session="sess-1"):
    return json.dumps({
        "type": "assistant", "timestamp": ts, "sessionId": session, "isSidechain": side, "effort": effort,
        "cwd": MARKERS[4], "gitBranch": MARKERS[2], "slug": MARKERS[3],
        "message": {"id": mid, "model": model, "role": "assistant",
                    "content": [{"type": "text", "text": MARKERS[0]}],
                    "usage": {"input_tokens": 2, "output_tokens": out, "cache_read_input_tokens": 1000,
                              "cache_creation_input_tokens": 50, "speed": "standard"}}})


def write_transcripts(home):
    proj = home / "projects" / f"-home-someone-{MARKERS[1]}"
    (proj / "s1" / "subagents").mkdir(parents=True)
    lines = [
        _line("msg_a", 10), _line("msg_a", 250),  # the same message streamed over two lines
        _line("msg_b", 40, effort="high", ts="2026-11-08T10:05:00.000Z"),
        json.dumps({"type": "assistant", "timestamp": "2026-11-08T10:06:00Z", "message": {
            "id": "x", "model": "<synthetic>", "usage": {"input_tokens": 0, "output_tokens": 0}}}),
        json.dumps({"type": "user", "message": {"role": "user", "content": MARKERS[0]}}),
        json.dumps({"type": "ai-title", "aiTitle": MARKERS[3]}),
        "not json at all",
    ]
    (proj / "s1.jsonl").write_text("\n".join(lines) + "\n")
    (proj / "s1" / "subagents" / "agent-1.jsonl").write_text(
        _line("msg_c", 7, model="claude-haiku-5-5", effort="medium", side=True) + "\n")
    return proj


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HLIDSKJALV_HOME", str(tmp_path / "hl"))
    home = tmp_path / "claude"
    proj = write_transcripts(home)
    db = tmp_path / "metrics.db"
    return {"home": home, "proj": proj, "db": db, "tmp": tmp_path}


def run(*argv):
    with pytest.raises(SystemExit) as e:
        main(["--config", "/nonexistent.yaml", *argv])
    return e.value.code


def cc_import(env, *extra):
    return run("plan", "import", "--claude-code", "--claude-home", str(env["home"]), "--db", str(env["db"]), *extra)


def rows(db):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM plan_usage ORDER BY model, effort")]
    finally:
        conn.close()


def test_one_row_per_message_with_the_largest_counts(env):
    assert cc_import(env) == 0
    got = rows(env["db"])
    assert [(r["model"], r["effort"], r["tokens_out"], r["agent"]) for r in got] == [
        ("claude-haiku-5-5", "medium", 7, 1),
        ("claude-opus-5-5", "high", 40, 0),
        ("claude-opus-5-5", "xhigh", 250, 0),
    ]
    a = next(r for r in got if r["effort"] == "xhigh")
    assert a["source"] == "claude-code" and a["tokens_measured"] == 1 and a["speed"] == "standard"
    assert (a["tokens_in"], a["cache_read"], a["cache_write"], a["thread_tokens"]) == (2, 1000, 50, 1052)
    b = next(r for r in got if r["effort"] == "high")
    assert (a["thread_turns"], b["thread_turns"]) == (1, 2) and a["thread_fp"] == b["thread_fp"]
    assert a["id"].startswith("cc-") and "msg_a" not in a["id"]


def test_repeat_import_adds_nothing_and_an_appended_file_is_completed(env, capsys):
    cc_import(env)
    before = rows(env["db"])
    capsys.readouterr()
    cc_import(env)
    assert rows(env["db"]) == before
    assert "0 transcript(s) read, 2 unchanged" in capsys.readouterr().out
    with open(env["proj"] / "s1.jsonl", "a") as f:
        f.write(_line("msg_b", 400, effort="high", ts="2026-11-08T10:05:01.000Z") + "\n")
    cc_import(env)
    assert "0 added, 1 completed" in capsys.readouterr().out
    assert next(r for r in rows(env["db"]) if r["effort"] == "high")["tokens_out"] == 400


def test_no_text_folder_branch_or_title_reaches_the_database(env):
    cc_import(env)
    data = env["db"].read_bytes()
    for marker in MARKERS + ["msg_a", "sess-1"]:
        assert marker.encode() not in data, marker


def test_dry_run_writes_nothing(env, capsys):
    assert cc_import(env, "--dry-run") == 0
    out = capsys.readouterr().out
    assert "2 transcript(s) read" in out and "claude-opus-5-5 xhigh" in out and "dry run" in out
    assert not env["db"].exists()
    assert not (env["tmp"] / "hl" / "plan-salt").exists()


def test_salt_is_private_and_stable(env):
    cc_import(env)
    salt = env["tmp"] / "hl" / "plan-salt"
    assert len(salt.read_text()) == 64
    if os.name == "posix":
        assert salt.stat().st_mode & 0o077 == 0
    ids = [r["id"] for r in rows(env["db"])]
    assert transcript_events(env["proj"] / "s1.jsonl", salt.read_text())[0]["id"] in ids


def test_events_file_keeps_contract_fields_only(env, capsys):
    f = env["tmp"] / "events.json"
    f.write_text(json.dumps({"format": "plan-usage.v1", "events": [
        {"id": "e1", "at": "2026-11-08T09:00:00Z", "source": "euboulia", "service": "claude", "model": "Opus 5.5",
         "effort": "max", "tokens_in_est": 300, "tokens_out_est": 900, "tokens_measured": False,
         "followed": True, "vote": -1, "prompt": MARKERS[0], "title": MARKERS[3]},
        {"id": "e1", "at": 1794128400000, "model": "Opus 5.5"},  # same id: nothing new
        {"id": "e2", "at": 1794128400, "model": "please summarise the whole contract for our client"},
        {"id": "e3", "at": 1794128400, "model": "Sonnet 5.5", "task": "write the email to " + MARKERS[1]},
    ]}))
    assert run("plan", "import", str(f), "--db", str(env["db"])) == 0
    out = capsys.readouterr().out
    assert "2 added" in out and "1 already there" in out and "1 rejected" in out
    got = {r["id"]: r for r in rows(env["db"])}
    assert set(got) == {"e1", "e3"}
    assert (got["e1"]["effort"], got["e1"]["tokens_out"], got["e1"]["followed"], got["e1"]["vote"]) == ("max", 900, 1, -1)
    assert got["e3"]["task"] is None
    data = env["db"].read_bytes()
    assert all(m.encode() not in data for m in MARKERS)


def test_clean_event_rejects_a_bad_file_shape(env, capsys):
    f = env["tmp"] / "bad.json"
    f.write_text(json.dumps([{"id": "e1"}]))
    assert run("plan", "import", str(f), "--db", str(env["db"])) == 2
    assert "plan-usage.v1" in capsys.readouterr().err
    assert clean_event({"id": "a b", "at": 1, "model": "m"})[0] is None


SNAPSHOTS = {"at": 1000.0 + 3 * 300, "limits": [{"kind": "weekly_all", "model": None, "percent": 2.0,
                                                  "resets_at": 999999.0}],
             "history": [
                 {"at": 1000.0, "limits": [{"kind": "weekly_all", "model": None, "percent": 10.0, "resets_at": 5000.0},
                                           {"kind": "weekly_scoped", "model": "Fable", "percent": 1.0, "resets_at": 5000.0}]},
                 {"at": 1300.0, "limits": [{"kind": "weekly_all", "model": None, "percent": 14.0, "resets_at": 5000.4}]},
                 {"at": 1600.0, "limits": [{"kind": "weekly_all", "model": None, "percent": 15.0, "resets_at": 5000.0},
                                           "junk"]},
             ]}


def test_meter_snapshots_import_once_and_rise_across_a_reset(env, capsys):
    f = env["tmp"] / "limits.json"
    f.write_text(json.dumps(SNAPSHOTS))
    assert run("plan", "import", "--meter", str(f), "--db", str(env["db"])) == 0
    assert "5 added, 0 already there, 1 rejected" in capsys.readouterr().out
    run("plan", "import", "--meter", str(f), "--db", str(env["db"]))
    assert "0 added, 5 already there" in capsys.readouterr().out
    conn = sqlite3.connect(env["db"])
    rise = {m["meter"]: m for m in meter_rise(conn, 0)}
    conn.close()
    # 10 -> 14 -> 15, then the window resets (percent drops, reset time moves): +2 from zero.
    assert (rise["weekly_all"]["rise"], rise["weekly_all"]["resets"], rise["weekly_all"]["readings"]) == (7.0, 1, 4)
    assert rise["weekly_scoped"]["model"] == "Fable" and rise["weekly_scoped"]["rise"] == 0


def test_flat_meter_format(tmp_path):
    f = tmp_path / "m.json"
    f.write_text(json.dumps({"format": "plan-meter.v1", "readings": [
        {"at": "2026-11-08T10:00:00Z", "meter": "session", "percent": 4},
        {"at": "2026-11-08T10:05:00Z", "meter": "session", "percent": -3}]}))
    readings, rejected = read_meter_file(f)
    assert len(readings) == 1 and rejected == 1 and readings[0]["model"] == ""
    f.write_text(json.dumps({"readings": []}))
    with pytest.raises(ValueError, match="plan-meter.v1"):
        read_meter_file(f)


def test_spend_and_call_usage_are_unchanged_by_plan_usage(env):
    db = MetricsDB(env["db"])
    db.record({"route": "code", "model": "claude-sonnet", "local": False, "status": 200, "latency_ms": 900,
               "usage": {"prompt_tokens": 100, "completion_tokens": 10}, "cost": 0.5})
    before = (db.spend_today(), db.spend_this_month(), usage(db.conn, 0))
    db.close()
    cc_import(env)
    db = MetricsDB(env["db"])
    assert (db.spend_today(), db.spend_this_month(), usage(db.conn, 0)) == before
    db.close()


def test_old_database_gains_the_plan_tables(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE calls (id INTEGER PRIMARY KEY, ts REAL NOT NULL, route TEXT NOT NULL, "
                 "model TEXT NOT NULL, local INTEGER NOT NULL, status INTEGER, latency_ms INTEGER, tokens_in INTEGER, "
                 "tokens_out INTEGER, cost REAL, fallbacks INTEGER NOT NULL DEFAULT 0, sensitive TEXT NOT NULL DEFAULT '[]')")
    conn.commit()
    conn.close()
    db = MetricsDB(path)
    assert plan_usage(db.conn, 0) == [] and meter_rise(db.conn, 0) == []
    db.close()


def test_usage_plan_report(env, capsys):
    cc_import(env)
    f = env["tmp"] / "limits.json"
    f.write_text(json.dumps(SNAPSHOTS))
    run("plan", "import", "--meter", str(f), "--db", str(env["db"]))
    capsys.readouterr()
    assert run("usage", "--plan", "--since", "1970-01-01", "--db", str(env["db"])) == 0
    out = capsys.readouterr().out
    assert "claude-opus-5-5 xhigh" in out and "total" in out and "weekly_all" in out and "+7 points" in out
    assert run("usage", "--plan", "--by", "agent", "--since", "2020-01-01", "--db", str(env["db"]), "--json") == 0
    doc = json.loads(capsys.readouterr().out)
    assert {r["key"]: r["messages"] for r in doc["rows"]} == {"main": 2, "subagent": 1}
    assert run("usage", "--plan", "--by", "route", "--db", str(env["db"])) == 2
    assert run("usage", "--by", "effort", "--db", str(env["db"])) == 2


def test_no_arguments_reads_the_config(env, tmp_path, capsys):
    assert run("plan", "import", "--db", str(env["db"])) == 0
    assert "nothing configured" in capsys.readouterr().out
    cfg = tmp_path / "config.yaml"
    meter = tmp_path / "limits.json"
    meter.write_text(json.dumps(SNAPSHOTS))
    cfg.write_text(f"plan:\n  meter_file: {meter}\n  person: alex\n")
    with pytest.raises(SystemExit) as e:
        main(["--config", str(cfg), "plan", "import", "--db", str(env["db"])])
    assert e.value.code == 0 and "5 added" in capsys.readouterr().out
    assert load_plan_settings(cfg).person == "alex"


def test_plan_settings_are_checked():
    with pytest.raises(ConfigError, match="unknown"):
        parse_plan({"claude_code": True, "metre_file": "x"})
    with pytest.raises(ConfigError, match="short label"):
        parse_plan({"person": "Alex from the client team"})
