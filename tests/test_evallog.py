"""`trioditis eval-routes --log`: the route picker against the router, counted from the decision log."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from trioditis import evallog
from trioditis.__main__ import main
from trioditis.config import parse_config
from trioditis_metrics import Prices

RAW = {
    "providers": {"local": {"base_url": "http://localhost:11434/v1", "local": True},
                  "cloud": {"base_url": "https://cloud.example/v1", "api_key_env": "CLOUD_KEY"}},
    "models": {"small": {"provider": "local", "id": "llama3.2:3b"},
               "cheap": {"provider": "cloud", "id": "cheap-1"},
               "big": {"provider": "cloud", "id": "big-1"}},
    "routes": {
        "code": {"description": "programming", "models": ["big"], "keywords": ["python"]},
        "reasoning": {"description": "hard problems", "models": ["big", "cheap"]},
        "quick": {"description": "short questions", "models": ["cheap"]},
        "private": {"description": "stays on this machine", "models": ["small"]},
    },
    "default_route": "quick",
    "logging": {"path": None},
}
PRICES = {"cheap": {"input": 1.0, "output": 2.0}, "big": {"input": 10.0, "output": 20.0}}
USAGE = {"prompt_tokens": 1000, "completion_tokens": 500}   # cheap: $0.002, big: $0.02


def cfg_of(**over):
    return parse_config({**json.loads(json.dumps(RAW)), **over})


def rec(route, router, outcome="chose", *, model=None, status=200, usage=USAGE, ms=900, local=False,
        ts="2026-11-20T10:00:00+0100", **extra):
    """One logged decision with the picker on. `route` is what was served."""
    picked = route if outcome == "chose" else None
    return {"ts": ts, "route": route, "model": model or RAW["routes"][route]["models"][0], "status": status,
            "local": local, "usage": usage, "router_route": router,
            "route_picker": {"model": "qwen2.5-coder:7b", "instruction": 1, "outcome": outcome, "route": picked,
                             "ms": ms if outcome in evallog.ASKED else None}, **extra}


def plain(route="quick", model="cheap", status=200, **extra):
    """A decision logged with the picker off, or a pinned follow-up turn."""
    return {"ts": "2026-11-19T09:00:00+0100", "route": route, "model": model, "status": status, "usage": USAGE, **extra}


def write_log(path: Path, records, junk: str = "") -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in records) + junk, encoding="utf-8")
    return path


@pytest.fixture
def prices(tmp_path):
    p = tmp_path / "prices.json"
    p.write_text(json.dumps(PRICES), encoding="utf-8")
    return Prices(p)


def report(tmp_path, records, capsys, prices, cfg=None, junk=""):
    log = write_log(tmp_path / "decisions.jsonl", records, junk)
    code = evallog.run(cfg or cfg_of(), SimpleNamespace(log=str(log), config="config.yaml"), prices=prices, today="2026-11-21")
    return code, capsys.readouterr().out


def flat(text: str) -> str:
    return " ".join(text.split())


MIXED = [
    plain(), plain(), plain("code"),
    rec("quick", "quick", ms=800), rec("code", "code", ms=1000), rec("quick", "quick", ms=1200),
    rec("reasoning", "quick", ms=1400), rec("reasoning", "quick", ms=1600), rec("code", "quick", ms=2000),
    rec("quick", "quick", "timeout", ms=2500), rec("quick", "quick", "no_route"),
    rec("code", "code", "skipped"), rec("quick", "quick", "busy"),
]


def test_outcomes_agreement_and_the_disagreement_table_on_a_hand_made_log(tmp_path, capsys, prices):
    code, out = report(tmp_path, MIXED, capsys, prices)
    text = flat(out)
    assert code == 0
    assert "13 logged requests; 10 were classified with the picker on (2026-11-20 to 2026-11-20)" in text
    assert "picker qwen2.5-coder:7b, instruction 1 (10 requests)" in text
    assert "What the picker did (n = 10)" in out
    for line in ("chose          6", "timeout        1", "no_route       1"):
        assert line in out
    assert "skipped        1   (tools matched a route" in out and "busy           1   (another picker call" in out
    assert "Asked 8 times, named a route: 6/8 = 75.0% (95% interval" in text
    assert "median 1.30 s, 95th percentile 1.90 s (n = 6)" in text
    assert "Where the picker chose (n = 6)" in out
    assert "same route as the router: 3/6 = 50.0% (95% interval" in text
    assert "quick -> reasoning       2" in out and "quick -> code            1" in out
    assert "not which of the two was right" in text


def test_disagreements_are_priced_as_served_and_on_the_routers_usual_model(tmp_path, capsys, prices):
    # Three disagreements, each served on big ($0.02) where the router's route, quick, answered with cheap ($0.002).
    _, out = report(tmp_path, MIXED, capsys, prices)
    text = flat(out)
    assert "What the 3 disagreements cost (an estimate)" in out
    assert "3 of 3 could be priced. As served: $0.0600." in text
    assert "model the router's route usually answered with: $0.0060. Difference: +$0.0540." in text
    assert "had no price" not in text


def test_the_routers_side_is_the_model_its_route_answered_with_in_the_log_not_the_first_listed(tmp_path, capsys, prices):
    # reasoning lists big first, but in this log it answered with cheap twice and big once (a fallback, or
    # `prefer: cheapest`). A failed reasoning call on big does not count as an answer.
    records = [plain("reasoning", model="cheap"), plain("reasoning", model="cheap"), plain("reasoning", model="big"),
               plain("reasoning", model="big", status=502), plain("reasoning", model="big", status=502),
               rec("quick", "reasoning")]                             # served cheap $0.002; reasoning's usual: cheap
    _, out = report(tmp_path, records, capsys, prices)
    assert "As served: $0.0020." in flat(out) and "usually answered with: $0.0020. Difference: +$0.0000." in flat(out)
    _, out = report(tmp_path, records[-1:], capsys, prices)           # no reasoning answers logged: first listed, big
    assert "usually answered with: $0.0200. Difference: -$0.0180." in flat(out)


def test_a_cheaper_pick_shows_a_negative_difference_and_a_local_route_costs_nothing(tmp_path, capsys, prices):
    records = [rec("quick", "code"),                                  # served cheap 0.002, router's big 0.02
               rec("private", "code", model="small", local=True)]    # served local 0, router's big 0.02
    _, out = report(tmp_path, records, capsys, prices)
    assert "As served: $0.0020." in flat(out) and "Difference: -$0.0380." in flat(out)


def test_what_cannot_be_priced_is_counted_and_explained(tmp_path, capsys, prices):
    records = [rec("reasoning", "quick"),
               rec("reasoning", "quick", status=502),                 # the call failed
               rec("reasoning", "quick", usage=None),                 # no token counts
               rec("reasoning", "quick", model="unlisted"),           # no price for the served model
               rec("reasoning", "gone")]                              # the router's route left the config
    _, out = report(tmp_path, records, capsys, prices)
    text = flat(out)
    assert "1 of 5 could be priced" in text and "4 had no price" in text
    assert "no longer in this config" in text
    _, out = report(tmp_path, records[1:], capsys, prices)
    assert "None could be priced." in out


def test_the_rotated_log_is_read_first_and_broken_lines_are_counted(tmp_path, capsys, prices):
    write_log(tmp_path / "decisions.jsonl.1", [rec("quick", "quick", ts="2026-11-18T08:00:00+0100")])
    code, out = report(tmp_path, [rec("code", "quick")], capsys, prices, junk='{"ts": "cut off\n[1, 2]\n\n')
    assert code == 0
    assert "decisions.jsonl.1 + " in flat(out) and "2 were classified with the picker on (2026-11-18 to 2026-11-20)" in flat(out)
    assert "2 lines were not valid JSON and were skipped" in out


def test_a_log_without_picker_decisions_says_what_to_do_and_exits_zero(tmp_path, capsys, prices):
    code, out = report(tmp_path, [plain(), plain()], capsys, prices)
    assert code == 0 and "2 logged requests; none was classified with the picker on." in out
    assert "Add a `route_picker:` block" in flat(out) and "What the picker did" not in out
    on = cfg_of(route_picker={"provider": "local", "model": "qwen2.5-coder:7b"})
    _, out = report(tmp_path, [plain()], capsys, prices, cfg=on)
    assert "The config has a `route_picker:` block" in flat(out) and "Add a" not in out


def test_a_picker_that_never_named_a_route_stops_before_the_agreement_block(tmp_path, capsys, prices):
    _, out = report(tmp_path, [rec("quick", "quick", "timeout"), rec("quick", "quick", "error")], capsys, prices)
    assert "Asked 2 times, named a route: 0/2 = 0.0%" in flat(out)
    assert "never named a route" in flat(out) and "Where the picker chose" not in out and "Time to answer" not in out


def test_cold_starts_are_counted_apart(tmp_path, capsys, prices):
    log = [rec("quick", "quick", "cold", ms=2500)] * 3 + [rec("quick", "quick", ms=900), rec("code", "quick", ms=700)]
    _, out = report(tmp_path, log, capsys, prices)
    text = flat(out)
    assert "cold 3 (no answer in time while the model was still loading)" in text
    assert "Asked 5 times, named a route: 2/5 = 40.0%" in text
    assert "Not counting cold starts: 2/2 = 100.0%" in text


def test_two_picker_models_are_listed_and_the_pooling_is_said(tmp_path, capsys, prices):
    other = rec("quick", "quick")
    other["route_picker"]["model"] = "llama3.2:3b"
    _, out = report(tmp_path, [rec("quick", "quick"), rec("code", "code"), other], capsys, prices)
    assert "picker qwen2.5-coder:7b, instruction 1 (2 requests)" in flat(out)
    assert "picker llama3.2:3b, instruction 1 (1 requests)" in flat(out) and "pool them" in out


def test_prompt_text_in_the_log_is_never_printed(tmp_path, capsys, prices):
    records = [rec("reasoning", "quick", prompt="SECRET-PROMPT-TEXT", reasons=["route picker chose SECRET-REASON"])]
    _, out = report(tmp_path, records, capsys, prices)
    assert "SECRET" not in out


def test_every_printed_line_fits_100_columns(tmp_path, capsys, prices):
    deep = tmp_path / ("d" * 60) / ("e" * 60)
    deep.mkdir(parents=True)
    for records in (MIXED, [plain()], [rec("reasoning", "quick", usage=None)]):
        _, out = report(deep, records, capsys, prices)
        assert all(len(line) <= 100 or " " not in line.strip() for line in out.splitlines()), out


def test_odd_records_do_not_crash_the_count(tmp_path, capsys, prices):
    records = [{"route": "quick", "router_route": "quick", "route_picker": "yes"},      # not a mapping: not counted
               {"route": "quick", "router_route": None, "route_picker": {"outcome": "chose"}},
               {"router_route": "quick", "route_picker": {}},                           # no outcome, no ts
               {"router_route": "quick", "route_picker": {"outcome": "chose", "route": "code", "ms": "fast"}}]
    code, out = report(tmp_path, records, capsys, prices)
    assert code == 0 and "2 were classified with the picker on (no dates)" in flat(out)
    assert "unknown" in out and "quick -> code" in out and "None could be priced." in out


# ------------------------------------------------------------------ the command line

def config_on_disk(tmp_path, **over):
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump({**json.loads(json.dumps(RAW)), **over}), encoding="utf-8")
    return p


@pytest.fixture
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("TRIODITIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TRIODITIS_ENV_FILE", str(tmp_path / "no.env"))


def test_the_command_line_reads_the_configs_log_or_a_named_one(tmp_path, capsys, _home):
    named = write_log(tmp_path / "other.jsonl", [rec("code", "quick")])
    own = write_log(tmp_path / "decisions.jsonl", [rec("quick", "quick"), rec("code", "code")])
    config = config_on_disk(tmp_path, logging={"path": str(own)})
    with pytest.raises(SystemExit) as stop:
        main(["eval-routes", "--log", "--config", str(config)])
    out = capsys.readouterr().out
    assert stop.value.code == 0 and "2 were classified with the picker on" in flat(out) and "2/2 = 100.0%" in out
    with pytest.raises(SystemExit) as stop:
        main(["--config", str(config), "eval-routes", "--log", str(named)])
    out = capsys.readouterr().out
    assert stop.value.code == 0 and "1 were classified with the picker on" in flat(out) and "quick -> code" in out


def test_log_errors_name_the_cause_and_the_fix_in_one_line(tmp_path, capsys, _home):
    off = config_on_disk(tmp_path)   # logging.path: null
    for argv, part in ((["--log"], "`logging.path` to nothing"),
                       (["--log", str(tmp_path / "missing.jsonl")], "no decision log at"),
                       (["--log", str(tmp_path)], "is a directory")):
        with pytest.raises(SystemExit) as stop:
            main(["--config", str(off), "eval-routes", *argv])
        assert isinstance(stop.value.code, str) and stop.value.code.startswith("eval-routes: ") and part in stop.value.code
        assert "\n" not in stop.value.code and capsys.readouterr().out == ""


def test_labels_options_are_refused_with_log_and_the_two_modes_exclude_each_other(tmp_path, capsys, _home):
    config = config_on_disk(tmp_path)
    log = write_log(tmp_path / "d.jsonl", [rec("quick", "quick")])
    for extra, name in ((["--dry-run"], "--dry-run"), (["--compare-threshold", "0.5"], "--compare-threshold"),
                        (["--notebook", str(tmp_path / "nb.jsonl")], "--notebook")):
        with pytest.raises(SystemExit) as stop:
            main(["--config", str(config), "eval-routes", "--log", str(log), *extra])
        assert isinstance(stop.value.code, str) and name in stop.value.code and "goes with --labels" in stop.value.code
    with pytest.raises(SystemExit) as stop:
        main(["--config", str(config), "eval-routes", "--log", str(log), "--labels", "x.csv"])
    assert stop.value.code == 2 and "not allowed with" in capsys.readouterr().err
    assert not (tmp_path / "nb.jsonl").exists()
