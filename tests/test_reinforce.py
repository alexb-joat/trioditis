"""The reinforcement loop's first half: the adopted-values file and its layers, the labelled gate,
`reinforce run --dry-run` and `reinforce revert`. No test here reaches a real server or the real data folder."""
import argparse
import dataclasses
import json
import os
import stat

import httpx
import pytest

from tests.test_trioditis import BASE, _env, body, make_client  # noqa: F401  (_env: the autouse fixture)
from trioditis import adopted, reinforce
from trioditis.adopted import PICKER_SERVES, THRESHOLD, AdoptedError, Layer
from trioditis.config import ConfigError, parse_config
from trioditis.router import PickerNoRoute, RoutePickerClient, decide
from trioditis_metrics.stats import paired_difference

AT = "2026-12-05T10:00:00+0100"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("TRIODITIS_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


def rcfg(tmp_path, *, on=True, picker=True, threshold=None, labels=None):
    raw = json.loads(json.dumps(BASE))
    raw["routes"]["code"].update(description="Writing or debugging code", examples=["fix this function"])
    raw["routes"]["quick"].update(description="Short questions", examples=["what time is it"])
    raw["embeddings"] = {"provider": "local", "model": "nomic-embed-text",
                         **({"threshold": threshold} if threshold is not None else {})}
    if picker:
        raw["route_picker"] = {"provider": "local", "model": "qwen2.5-coder:7b", "timeout_ms": 200}
    if on:
        raw["reinforce"] = {"labels": str(labels or tmp_path / "labels.csv")}
    return parse_config(raw)


def labels_file(tmp_path, rows):
    p = tmp_path / "labels.csv"
    p.write_text("prompt,expected_route\n" + "".join(f"{q},{r}\n" for q, r in rows), encoding="utf-8")
    return p


class Sims:
    """An embedder that answers from a table: prompt -> similarity to `code` (quick gets 0.1)."""
    def __init__(self, table):
        self.table = table

    async def similarities(self, text):
        return {"code": self.table[text], "quick": 0.1}


class Picker:
    def __init__(self, table):
        self.table, self.asked = table, []

    async def pick(self, text):
        self.asked.append(text)
        if self.table[text] is None:
            raise PickerNoRoute("reply named no route")
        return self.table[text]


def ns(**kw):
    return argparse.Namespace(**{"dry_run": False, "all": False, **kw})


# ------------------------------------------------------------------ config

def test_absent_block_is_off(tmp_path):
    assert rcfg(tmp_path, on=False).reinforce is None


@pytest.mark.parametrize("block, msg", [
    ("yes", "must be a mapping"),
    ({"labels": "x.csv", "margin": 1}, "unknown key(s) margin"),
    ({}, "reinforce.labels must be the path"),
    ({"labels": 3}, "reinforce.labels must be the path"),
])
def test_bad_block_is_refused(block, msg):
    raw = json.loads(json.dumps(BASE))
    raw["reinforce"] = block
    with pytest.raises(ConfigError, match=msg.replace("(", r"\(").replace(")", r"\)")):
        parse_config(raw)


def test_threshold_knows_whether_the_config_named_it(tmp_path):
    assert rcfg(tmp_path).embeddings.threshold_given is False
    assert rcfg(tmp_path, threshold=0.5).embeddings.threshold_given is True


# ------------------------------------------------------------------ the interval

def test_paired_difference_matches_a_hand_calculation():
    diff, low, high = paired_difference(12, 8, 120)
    assert diff == pytest.approx(4 / 120)
    # (12.5 - 8.5) / 122 = 0.03279; 1.96 * sqrt(21 - 16/122) / 122 = 0.07339
    assert low == pytest.approx(-0.04060, abs=1e-4) and high == pytest.approx(0.10618, abs=1e-4)


def test_paired_difference_has_width_with_no_disagreement_and_refuses_nonsense():
    diff, low, high = paired_difference(0, 0, 50)
    assert diff == 0 and low < 0 < high
    assert paired_difference(0, 0, 0) is None
    with pytest.raises(ValueError):
        paired_difference(5, 6, 10)


# ------------------------------------------------------------------ the adopted file

def test_no_file_is_an_empty_document(home):
    assert adopted.read() == adopted.empty()


@pytest.mark.parametrize("text, msg", [
    ("not json", "is not JSON"),
    ('{"schema": "other"}', "is not a trioditis-adopted.v1 document"),
    ('{"schema": "trioditis-adopted.v1", "values": []}', "lacks its values or history"),
    ('{"schema": "trioditis-adopted.v1", "history": [], "values": {"embeddings.threshold": {"value": 0.9}}}',
     "outside 0.35 to 0.55"),
    ('{"schema": "trioditis-adopted.v1", "history": [], "values": {"route_picker.serves": {"value": "no"}}}',
     "must be true or false"),
    ('{"schema": "trioditis-adopted.v1", "history": [], "values": {"limits.max_fallbacks": {"value": 9}}}',
     "is not a value the loop may set"),
])
def test_unusable_file_says_why_and_what_to_do(home, text, msg):
    home.mkdir()
    (home / "trioditis-adopted.json").write_text(text)
    with pytest.raises(AdoptedError, match=msg) as e:
        adopted.read()
    assert "fix or delete" in str(e.value) and "config.yaml values are in force" in str(e.value)


def test_adopt_then_apply_and_the_file_is_private(tmp_path, home):
    cfg = rcfg(tmp_path)
    doc = adopted.adopt(cfg, adopted.empty(), THRESHOLD, 0.40, at=AT, gate={"n": 105}, reason="test")
    doc = adopted.adopt(cfg, doc, PICKER_SERVES, False, at=AT, gate=None, reason="test")
    adopted.write(doc)
    path = home / "trioditis-adopted.json"
    if os.name == "posix":  # Windows has no owner-only mode bits
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    live = adopted.apply(cfg, adopted.read())
    assert live.embeddings.threshold == 0.40 and live.route_picker.serves is False
    assert cfg.embeddings.threshold == 0.45 and cfg.route_picker.serves is True  # the config itself is untouched
    first = doc["history"][0]
    assert (first["before"], first["after"], first["gate"]) == (0.45, 0.40, {"n": 105})


def test_loop_off_ignores_adopted_values(tmp_path):
    on, off = rcfg(tmp_path), rcfg(tmp_path, on=False)
    doc = adopted.adopt(on, adopted.empty(), THRESHOLD, 0.40, at=AT, gate=None, reason="test")
    assert adopted.apply(off, doc) is off
    assert adopted.layer_lines(off, doc) == ["embeddings.threshold 0.45: from the built-in default",
                                             "route picker serves yes: from config.yaml"]


@pytest.mark.parametrize("setting, value, msg", [
    (THRESHOLD, 0.35, "moves at most 0.05 a step"),
    (THRESHOLD, 0.60, "outside 0.35 to 0.55"),
    (THRESHOLD, 0.45, "is already 0.45"),
    (PICKER_SERVES, True, "is already yes"),
    ("limits.max_fallbacks", 9, "is not a value the loop may set"),
])
def test_adopt_refuses_what_the_limits_forbid(tmp_path, setting, value, msg):
    with pytest.raises(AdoptedError, match=msg):
        adopted.adopt(rcfg(tmp_path), adopted.empty(), setting, value, at=AT, gate=None, reason="test")


def test_adopt_needs_the_part_of_the_config_it_changes(tmp_path):
    with pytest.raises(AdoptedError, match="nothing for route_picker.serves"):
        adopted.adopt(rcfg(tmp_path, picker=False), adopted.empty(), PICKER_SERVES, False, at=AT, gate=None, reason="t")


def test_revert_takes_back_the_last_change_then_the_one_before(tmp_path):
    cfg = rcfg(tmp_path)
    doc = adopted.adopt(cfg, adopted.empty(), THRESHOLD, 0.40, at=AT, gate=None, reason="a")
    doc = adopted.adopt(cfg, doc, THRESHOLD, 0.35, at=AT, gate=None, reason="b")
    doc, [e] = adopted.revert(cfg, doc, at=AT, reason="by hand")
    assert (e["before"], e["after"]) == (0.35, 0.40) and doc["values"][THRESHOLD]["value"] == 0.40
    doc, [e] = adopted.revert(cfg, doc, at=AT, reason="by hand")
    assert (e["before"], e["after"]) == (0.40, 0.45) and doc["values"] == {}
    assert adopted.revert(cfg, doc, at=AT, reason="by hand") == (doc, [])
    assert [(r["setting"], r["value"]) for r in doc["reverted"]] == [(THRESHOLD, 0.35), (THRESHOLD, 0.40)]


def test_revert_all_returns_to_the_config_and_later_adoptions_revert_alone(tmp_path):
    cfg = rcfg(tmp_path)
    doc = adopted.adopt(cfg, adopted.empty(), THRESHOLD, 0.40, at=AT, gate=None, reason="a")
    doc = adopted.adopt(cfg, doc, PICKER_SERVES, False, at=AT, gate=None, reason="b")
    doc, entries = adopted.revert(cfg, doc, at=AT, everything=True, reason="by hand")
    assert doc["values"] == {} and [(e["setting"], e["after"]) for e in entries] == [(PICKER_SERVES, True), (THRESHOLD, 0.45)]
    doc = adopted.adopt(cfg, doc, THRESHOLD, 0.50, at=AT, gate=None, reason="c")
    doc, [e] = adopted.revert(cfg, doc, at=AT, reason="by hand")
    assert e["before"] == 0.50 and doc["values"] == {}
    assert adopted.revert(cfg, doc, at=AT, reason="by hand")[1] == []


def test_layer_lines_name_the_layer(tmp_path):
    cfg = rcfg(tmp_path, threshold=0.5)
    doc = adopted.adopt(cfg, adopted.empty(), THRESHOLD, 0.45, at=AT, gate=None, reason="a")
    assert adopted.layer_lines(cfg, doc) == ["embeddings.threshold 0.45: adopted 2026-12-05 (config.yaml has 0.5)",
                                             "route picker serves yes: from config.yaml"]


def test_running_router_sees_a_change_without_a_restart_and_survives_a_broken_file(tmp_path, home):
    cfg = rcfg(tmp_path)
    layer = Layer(cfg, every_s=0)
    assert layer.current() is cfg
    adopted.write(adopted.adopt(cfg, adopted.empty(), THRESHOLD, 0.40, at=AT, gate=None, reason="a"))
    assert layer.current().embeddings.threshold == 0.40
    (home / "trioditis-adopted.json").write_text("{broken")
    assert layer.current() is cfg and "is not JSON" in layer.error
    (home / "trioditis-adopted.json").unlink()
    assert layer.current() is cfg and layer.error is None


def test_layer_never_opens_the_file_with_the_loop_off(tmp_path, home):
    home.mkdir()
    (home / "trioditis-adopted.json").write_text("{broken")
    off = rcfg(tmp_path, on=False)
    layer = Layer(off, every_s=0)
    assert layer.current() is off and layer.error is None


# ------------------------------------------------------------------ the router with an adopted value

@pytest.mark.anyio
async def test_router_serves_its_own_route_when_picker_serving_is_adopted_off(tmp_path):
    cfg = rcfg(tmp_path)
    doc = adopted.adopt(cfg, adopted.empty(), PICKER_SERVES, False, at=AT, gate=None, reason="a")
    live = adopted.apply(cfg, doc)

    def never(req):
        raise AssertionError("the picker must not be asked")

    picker = RoutePickerClient(live, httpx.AsyncClient(transport=httpx.MockTransport(never)))
    d = await decide(live, body("fix the python bug"), picker=picker)
    assert d.route == d.router_route == "code" and d.picker["outcome"] == "not_serving"
    assert any("an adopted setting has the router serve" in r for r in d.reasons)


def test_route_endpoint_uses_the_adopted_threshold(tmp_path, home):
    cfg = rcfg(tmp_path, picker=False)
    adopted.write(adopted.adopt(cfg, adopted.empty(), THRESHOLD, 0.40, at=AT, gate=None, reason="a"))

    def handler(req):  # the embeddings provider: 0.42 for the prompt, between the adopted 0.40 and the config's 0.45
        texts = json.loads(req.content)["input"]
        vec = {"fix this function": [1.0, 0.0, 0.0], "what time is it": [0.0, 1.0, 0.0]}
        return httpx.Response(200, json={"data": [{"index": i, "embedding": vec.get(t, [0.42, 0.0, 0.9075])}
                                                  for i, t in enumerate(texts)]})

    with make_client(handler, config=cfg) as client:
        assert client.post("/v1/route", json=body("please look at this")).json()["route"] == "code"
    with make_client(handler, config=rcfg(tmp_path, picker=False, on=False)) as client:
        assert client.post("/v1/route", json=body("please look at this")).json()["route"] == "quick"


# ------------------------------------------------------------------ the gate

def test_gate_passes_only_when_ahead_with_the_interval_above_the_margin():
    def rows(both, b, c, neither):
        return ([True] * both + [False] * b + [True] * c + [False] * neither,
                [True] * both + [True] * b + [False] * c + [False] * neither)

    v = reinforce.gate(*rows(60, 12, 8, 40))
    assert v.passed and (v.n, v.other_right, v.candidate_right, v.only_candidate, v.only_other) == (120, 68, 72, 12, 8)
    v = reinforce.gate(*rows(60, 11, 9, 40))  # ahead by 2 of 120: the interval reaches -5.7 points
    assert not v.passed and "ahead, but the interval reaches -5.7 points" in v.why
    assert reinforce.gate(*rows(60, 5, 5, 50)).why == "it is right on the same number of rows"
    assert reinforce.gate(*rows(60, 3, 9, 48)).why == "it is right on fewer rows"
    with pytest.raises(ValueError):
        reinforce.gate([True], [True, False])


GATE_ROWS = [("fix my loop", "code"), ("sort this list", "code"), ("capital of France", "quick"),
             ("why is the sky blue", "quick"), ("x" * 300, "code")]
SIMS = {"fix my loop": 0.50, "sort this list": 0.42, "capital of France": 0.20, "why is the sky blue": 0.47,
        "x" * 300: 0.30}
PICKS = {"fix my loop": "code", "sort this list": "code", "capital of France": "quick", "why is the sky blue": None}


@pytest.mark.anyio
async def test_collect_asks_the_picker_only_where_the_router_would(tmp_path):
    cfg = rcfg(tmp_path)
    rows = reinforce.read_labels(labels_file(tmp_path, GATE_ROWS), cfg.routes)
    picker = Picker(PICKS)
    facts = await reinforce.collect(cfg, rows, [0.45, 0.40], Sims(SIMS), picker)
    assert "x" * 300 not in picker.asked and (facts.picker_asked, facts.picker_no_route) == (4, 1)
    assert facts.router[0.45] == ["code", "quick", "quick", "code", "quick"]
    assert facts.router[0.40] == ["code", "code", "quick", "code", "quick"]
    # the router alone: rows 1 and 3 right; with the picker serving: rows 1, 2 and 3 (row 4 falls back to the router)
    assert facts.right(0.45, False) == [True, False, True, False, False]
    assert facts.right(0.45, True) == [True, True, True, False, False]


@pytest.mark.anyio
async def test_with_long_prompts_cut_collect_asks_with_the_first_max_chars_characters(tmp_path):
    cfg = rcfg(tmp_path)
    cfg = dataclasses.replace(cfg, route_picker=dataclasses.replace(cfg.route_picker, long_prompts="cut"))
    rows = reinforce.read_labels(labels_file(tmp_path, GATE_ROWS), cfg.routes)
    picker = Picker({**PICKS, "x" * 200: "code"})
    facts = await reinforce.collect(cfg, rows, [0.45], Sims(SIMS), picker)
    assert picker.asked[-1] == "x" * 200 and (facts.picker_asked, facts.picker_no_route) == (5, 1)
    assert facts.picker[4] == "code"


def test_dry_run_reports_the_verdict_and_writes_nothing(tmp_path, home, capsys):
    cfg = rcfg(tmp_path, labels=labels_file(tmp_path, GATE_ROWS))
    assert reinforce.run(cfg, ns(dry_run=True), embedder=Sims(SIMS), picker=Picker(PICKS)) == 0
    out = capsys.readouterr().out
    assert "gate set " in out and ": 5 rows, sha256 " in out
    assert "in force: embeddings.threshold 0.45: from the built-in default; route picker serves yes: from config.yaml" in out
    assert "right on 3 of 5 (60.0%" in out
    assert "candidate: route picker serves no" in out
    assert "the picker was asked on 4 of 5 rows" in out and "named no route on 1" in out
    assert "only the candidate right on 0 row(s), only the setting in force on 1" in out
    assert "does not pass: it is right on fewer rows" in out
    assert "embeddings.threshold: no candidate" in out
    assert out.rstrip().endswith("would adopt: nothing\ndry run: nothing written")
    assert not home.exists()
    assert "fix my loop" not in out  # no prompt text in the report


def test_run_without_dry_run_adopts_nothing_and_says_why(tmp_path, home):
    cfg = rcfg(tmp_path, labels=labels_file(tmp_path, GATE_ROWS))
    with pytest.raises(SystemExit) as e:
        reinforce.run(cfg, ns(), embedder=Sims(SIMS), picker=Picker(PICKS))
    assert "adopting is not in this version" in str(e.value) and "--dry-run" in str(e.value)
    assert not home.exists()


def test_run_with_the_loop_off_or_no_gate_set_says_what_to_do(tmp_path):
    with pytest.raises(SystemExit, match="the loop is off: this config has no `reinforce:` block"):
        reinforce.run(rcfg(tmp_path, on=False), ns(dry_run=True))
    with pytest.raises(SystemExit, match=r"no gate set at .*labels.csv \(config reinforce.labels\)"):
        reinforce.run(rcfg(tmp_path), ns(dry_run=True), embedder=Sims(SIMS), picker=Picker(PICKS))


def test_candidates_respect_the_batch(tmp_path):
    cfg, sha = rcfg(tmp_path), "abc"
    assert reinforce.candidates(cfg, adopted.empty(), sha)[0] == [(PICKER_SERVES, False)]
    decided = {**adopted.empty(), "batch": {"sha256": sha, "decided": [{"setting": PICKER_SERVES}]}}
    assert reinforce.candidates(cfg, decided, sha)[0] == []
    assert reinforce.candidates(cfg, decided, "a-new-batch")[0] == [(PICKER_SERVES, False)]
    spent = {**adopted.empty(), "batch": {"sha256": sha, "decided": [{"setting": THRESHOLD}] * 5}}
    out, notes = reinforce.candidates(cfg, spent, sha)
    assert out == [] and "is spent" in notes[0]
    back = {**adopted.empty(), "reverted": [{"setting": PICKER_SERVES, "value": False, "batch": sha}]}
    out, notes = reinforce.candidates(cfg, back, sha)
    assert out == [] and "was taken back" in notes[0]
    out, notes = reinforce.candidates(rcfg(tmp_path, picker=False), adopted.empty(), sha)
    assert out == [] and "no `route_picker:` block" in notes[0]


# ------------------------------------------------------------------ revert and check

def test_revert_command_writes_the_file_and_the_notebook(tmp_path, home, capsys):
    cfg = rcfg(tmp_path)
    adopted.write(adopted.adopt(cfg, adopted.empty(), THRESHOLD, 0.40, at=AT, gate=None, reason="a"))
    book = tmp_path / "notebook.jsonl"
    assert reinforce.revert(cfg, ns(dry_run=True), notebook=book) == 0
    assert "dry run: nothing written" in capsys.readouterr().out and not book.exists()
    assert adopted.read()["values"][THRESHOLD]["value"] == 0.40
    assert reinforce.revert(cfg, ns(), notebook=book) == 0
    assert "embeddings.threshold: 0.4 back to 0.45 (adopted 2026-12-05)" in capsys.readouterr().out
    assert adopted.read()["values"] == {}
    line = json.loads(book.read_text())
    assert (line["kind"], line["before"], line["after"]) == ("routing_revert", 0.40, 0.45)
    assert reinforce.revert(cfg, ns(), notebook=book) == 0
    assert "nothing to take back" in capsys.readouterr().out


def test_check_lines(tmp_path, home):
    off = rcfg(tmp_path, on=False)
    assert reinforce.check_lines(off) == []
    cfg = rcfg(tmp_path)
    lines = reinforce.check_lines(cfg)
    assert lines[0].startswith("reinforce: on; gate set ") and "NOT usable (no gate set at " in lines[0]
    labels_file(tmp_path, GATE_ROWS)
    adopted.write(adopted.adopt(cfg, adopted.empty(), THRESHOLD, 0.40, at=AT, gate=None, reason="a"))
    lines = reinforce.check_lines(cfg)
    assert ": 5 rows" in lines[0]
    assert lines[1] == "  embeddings.threshold 0.4: adopted 2026-12-05 (the built-in default has 0.45)"
    assert "ignored and the config.yaml values serve" in reinforce.check_lines(off)[0]
    assert "no `route_picker:` block" in reinforce.check_lines(rcfg(tmp_path, picker=False))[-1]
