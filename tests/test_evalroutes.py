"""`trioditis eval-routes`: grades the real classifier on a labelled CSV. No network, nothing outside tmp_path."""

import asyncio
import hashlib
import json
import math
import random
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml

from trioditis import evalroutes
from trioditis.__main__ import main
from trioditis.config import parse_config
from trioditis.evalroutes import Candidate, Evaluation, LabelsError, Memo, Row, Score, Sweep, read_labels, split
from trioditis.router import EmbeddingsCountMismatch, _classify, decide

RAW = {
    "providers": {"local": {"base_url": "http://localhost:11434/v1", "local": True}},
    "models": {"small": {"provider": "local", "id": "llama3.2:3b"}},
    "routes": {
        "code": {"models": ["small"], "keywords": ["python"], "examples": ["a code example"]},
        "writing": {"models": ["small"], "keywords": ["email"], "examples": ["a writing example"]},
        "quick": {"models": ["small"], "examples": ["a quick example"]},
    },
    "default_route": "quick",
    "embeddings": {"provider": "local", "model": "fake-embed", "threshold": 0.55},
    "logging": {"path": None},
}
ROUTES = ["code", "writing", "quick"]


def flat(text: str) -> str:
    """The printed text with every run of whitespace as one space: a wrapped line reads as it would unwrapped."""
    return " ".join(text.split())

def raw(**over):
    out = json.loads(json.dumps(RAW))
    out.update(over)
    return out


def cfg_of(**over):
    return parse_config(raw(**over))


class FakeEmbedder:
    """Answers from a table of similarities; counts the questions it was asked."""

    def __init__(self, table: dict[str, dict[str, float]] | None = None):
        self.table = table or {}
        self.calls: list[str] = []

    async def similarities(self, text: str) -> dict[str, float]:
        self.calls.append(text)
        return self.table.get(text, {"code": 0.1, "writing": 0.1, "quick": 0.1})


class Down:
    async def similarities(self, text: str):
        raise ConnectionError("nobody home")


def write_csv(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8", newline="")
    return path


# Four rows the keyword rule gets right or wrong on purpose, plus two decided by similarity.
HAND = [
    ("fix my python script", "code"),       # keyword: right
    ("write an email to Sam", "writing"),   # keyword: right
    ("python email parser", "code"),        # tie on keywords, first in config order wins: code, right
    ("hello there", "quick"),               # nothing matches: default, right
    ("python tips", "writing"),             # keyword says code: wrong (code predicted for a writing row)
    ("a semantic code question", "code"),   # no keyword; embeddings say code at 0.7
]
HAND_TABLE = {"a semantic code question": {"code": 0.7, "writing": 0.2, "quick": 0.3}}


def hand_csv(tmp_path):
    body = "prompt,expected_route\n" + "".join(f'"{p}",{r}\n' for p, r in HAND)
    return write_csv(tmp_path / "labels.csv", body)


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    """The notebook lives under HLIDSKJALV_HOME: keep it in tmp_path so no test touches the real one."""
    monkeypatch.setenv("HLIDSKJALV_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TRIODITIS_ENV_FILE", str(tmp_path / "no.env"))  # main() would load the repo's .env into os.environ


def args_for(labels, tmp_path, **kw):
    return SimpleNamespace(labels=str(labels), config="config.yaml", notebook=str(tmp_path / "notebook.jsonl"),
                           dry_run=False, **kw)


# ------------------------------------------------------------------ the labels file

def test_extra_columns_blank_lines_a_bom_and_multiline_prompts_are_fine(tmp_path):
    p = tmp_path / "l.csv"
    p.write_bytes("﻿id,prompt,expected_route,note\n1,\"two\nlines\",code,x\n\n2,hello,quick\n".encode())
    rows = read_labels(p, ROUTES)
    assert [(r.number, r.prompt, r.expected) for r in rows] == [(2, "two\nlines", "code"), (4, "hello", "quick")]


@pytest.mark.parametrize("content, parts", [
    (None, ["not found", "--labels"]),
    ("", ["is empty", "header row"]),
    ("prompt,route\nhi,quick\n", ["row 1", "no column named 'expected_route'", "found: prompt, route"]),
    ("id,text\n1,hi\n", ["row 1", "does not look like a header", "neither a 'prompt' nor an 'expected_route'", "found: id, text"]),
    ("prompt,expected_route\n", ["no rows", "expected route"]),
    ("prompt,expected_route\nhi,quick\n  ,code\n", ["row 3", "prompt is empty", "delete the row"]),
    ("prompt,expected_route\nhi,quick\nbye,cdoe\n", ["row 3", "'cdoe'", "code, writing, quick", "did you mean 'code'"]),
    ("prompt,expected_route\nhi\n", ["row 2 has no expected_route", "1 column"]),
    ("prompt,expected_route\nhi,\n", ["row 2", "expected_route is empty", "code, writing, quick"]),
    ("prompt,expected_route\nhi,quick\nhi ,code\n", ["rows 2 and 3 have the same prompt", "remove one"]),
    ("prompt,expected_route\nhi,quick\n\"never closed,code\nmore,quick\n", ["row 3", "quote is opened and never closed", "twice"]),
    ("prompt,expected_route\n\"said \"hi\" to me\",quick\n", ["row 2", "twice"]),
    (b"prompt,expected_route\n\xff\xfe,code\n", ["not UTF-8", "save it as CSV UTF-8"]),
])
def test_label_file_errors_name_the_cause_the_place_and_the_fix(tmp_path, content, parts):
    p = tmp_path / "labels.csv"
    if isinstance(content, bytes):
        p.write_bytes(content)
    elif content is not None:
        write_csv(p, content)
    with pytest.raises(LabelsError) as err:
        read_labels(p, ROUTES)
    msg = str(err.value)
    assert all(part in msg for part in parts), msg
    assert "\n" not in msg and "Traceback" not in msg
    if content is not None:
        assert str(p) in msg  # the place


def test_a_directory_instead_of_a_file_is_an_error_not_a_traceback(tmp_path):
    with pytest.raises(LabelsError, match="cannot read labels file"):
        read_labels(tmp_path, ROUTES)


def test_several_unknown_routes_are_counted(tmp_path):
    p = write_csv(tmp_path / "l.csv", "prompt,expected_route\na,x\nb,y\nc,quick\n")
    with pytest.raises(LabelsError, match=r"row 2.*1 more row"):
        read_labels(p, ROUTES)


# ------------------------------------------------------------------ the numbers

def graded(tmp_path, embedder=None, **over):
    cfg = cfg_of(**over)
    rows = read_labels(hand_csv(tmp_path), cfg.routes)
    return cfg, rows, asyncio.run(evalroutes.evaluate(cfg, rows, embedder))


def test_accuracy_per_route_and_confusion_on_a_hand_made_set(tmp_path):
    cfg, rows, ev = graded(tmp_path, FakeEmbedder(HAND_TABLE))
    kw = ev.keywords
    assert kw.n == len(HAND) == len(rows) == 6
    assert kw.predicted == ["code", "writing", "code", "quick", "code", "quick"]
    assert (kw.k, kw.per_route("code"), kw.per_route("writing"), kw.per_route("quick")) == (4, (2, 3), (1, 2), (1, 1))
    assert kw.confusion("writing", "code") == 1 and kw.confusion("code", "quick") == 1 and kw.confusion("code", "code") == 2
    emb = ev.embeddings
    assert emb.predicted[-1] == "code" and emb.k == 5  # the similarity of 0.7 clears the 0.55 threshold
    assert emb.predicted[:5] == kw.predicted[:5]       # the others are decided by keywords or the default


def test_the_report_prints_n_and_k_over_n_with_an_interval(tmp_path):
    cfg, rows, ev = graded(tmp_path, FakeEmbedder(HAND_TABLE))
    text = "\n".join(evalroutes.format_report(cfg, ev, "labels.csv", len(rows), "config.yaml", "2026-11-13"))
    assert "n = 6 labelled prompts" in text
    assert "accuracy 4/6 = 66.7%  (95% interval 30.0-90.3%)" in text
    assert "accuracy 5/6 = 83.3%" in text


def test_every_printed_line_fits_100_columns(tmp_path, capsys):
    table, labels = sweep_set(tmp_path)
    evalroutes.run(cfg_of(), args_for(labels, tmp_path), embedder=FakeEmbedder(table))
    evalroutes.run(cfg_of(), args_for(hand_csv(tmp_path), tmp_path), embedder=FakeEmbedder(HAND_TABLE))  # the skipped sweep
    lines = capsys.readouterr().out.splitlines()
    assert any("disagree on" in x for x in lines) and any("skipped" in x for x in lines)
    # A path is never broken, so a line that holds one may run long; the temporary folder's length varies by system.
    assert all(len(x) <= 100 for x in lines if str(tmp_path) not in x and not x.startswith(("labels:", "config:")))


def test_it_grades_the_routers_own_choice(tmp_path):
    """The graded route is the route `decide()` reports for the same request: no copy of the logic."""
    cfg, rows, ev = graded(tmp_path, FakeEmbedder(HAND_TABLE))
    fake = FakeEmbedder(HAND_TABLE)
    for r, got in zip(rows, ev.embeddings.predicted, strict=True):
        d = asyncio.run(decide(cfg, {"model": "auto", "messages": [{"role": "user", "content": r.prompt}]}, embedder=fake))
        assert d.route == got


def test_keywords_only_when_the_config_has_no_embeddings(tmp_path):
    raw_cfg = raw()
    del raw_cfg["embeddings"]
    cfg = parse_config(raw_cfg)
    rows = read_labels(hand_csv(tmp_path), cfg.routes)
    ev = asyncio.run(evalroutes.evaluate(cfg, rows, FakeEmbedder()))
    assert ev.embeddings is None and ev.sweep is None and "no `embeddings:` block" in ev.embeddings_note


def test_unreachable_embeddings_keep_the_keyword_row_and_say_so(tmp_path, capsys):
    cfg, rows, ev = graded(tmp_path, Down())
    assert ev.embeddings is None and ev.sweep is None and ev.keywords.n == 6
    assert "did not answer (ConnectionError)" in ev.embeddings_note and "ollama pull fake-embed" in ev.embeddings_note
    code = evalroutes.run(cfg, args_for(hand_csv(tmp_path), tmp_path), embedder=Down())
    out = capsys.readouterr().out
    assert code == 0 and "Keywords only" in out and "not graded" in out and "Threshold sweep" not in out


def short_embedder(cfg):
    """The real Embedder against a server that returns one vector whatever it is sent."""
    def handler(request):
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 0.0]}]})
    return evalroutes.Embedder(cfg, httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def two_examples():
    raw_cfg = raw()
    raw_cfg["routes"]["code"]["examples"] = ["a code example", "another code example"]
    return parse_config(raw_cfg)


def test_a_reply_with_fewer_vectors_than_texts_is_an_error_not_a_grade(tmp_path):
    cfg = two_examples()
    with pytest.raises(EmbeddingsCountMismatch, match="returned 1 vectors for 2 texts"):
        asyncio.run(short_embedder(cfg).similarities("anything"))
    rows = read_labels(hand_csv(tmp_path), cfg.routes)
    ev = asyncio.run(evalroutes.evaluate(cfg, rows, short_embedder(cfg)))
    assert ev.embeddings is None and ev.sweep is None and ev.keywords.n == 6
    assert "not graded" in ev.embeddings_note and "returned 1 vectors for 2 texts" in ev.embeddings_note


def test_the_router_falls_back_to_keywords_on_a_short_embeddings_reply():
    cfg = two_examples()
    reasons = []
    route = asyncio.run(_classify(cfg, "fix my python script", short_embedder(cfg), reasons))
    assert route == "code" and reasons[0] == "embeddings unavailable (EmbeddingsCountMismatch); keywords only"


def test_embeddings_with_no_route_examples_are_not_graded(tmp_path):
    raw_cfg = raw()
    for r in raw_cfg["routes"].values():
        del r["examples"]
    cfg = parse_config(raw_cfg)
    rows = read_labels(hand_csv(tmp_path), cfg.routes)
    ev = asyncio.run(evalroutes.evaluate(cfg, rows, evalroutes.Embedder(cfg, None)))  # never reaches the network: no examples
    assert ev.embeddings is None and "examples" in ev.embeddings_note


def test_every_prompt_is_embedded_once_however_many_thresholds_are_tried(tmp_path):
    fake = FakeEmbedder(HAND_TABLE)
    graded(tmp_path, fake)
    assert sorted(fake.calls) == sorted({p for p, _ in HAND})


def test_the_memo_remembers_answers_but_not_failures():
    class Flaky:
        n = 0

        async def similarities(self, text):
            self.n += 1
            if self.n == 1:
                raise ConnectionError
            return {"a": 1.0}

    memo = Memo(Flaky())
    with pytest.raises(ConnectionError):
        asyncio.run(memo.similarities("x"))
    assert asyncio.run(memo.similarities("x")) == {"a": 1.0} == asyncio.run(memo.similarities("x"))
    assert memo.inner.n == 2


# ------------------------------------------------------------------ the sweep

def semantic_set(tmp_path, n_code, n_quick, code_sim=0.48, name="sweep.csv"):
    """Prompts only embeddings can place. A code prompt scores `code_sim` for code (right at thresholds up to it,
    wrong above); a quick one scores 0.2 for code, so it stays on quick at every threshold."""
    table, lines = {}, ["prompt,expected_route"]
    for i in range(n_code):
        t = f"describe thing number {i}"
        table[t] = {"code": code_sim, "writing": 0.1, "quick": 0.1}
        lines.append(f"{t},code")
    for i in range(n_quick):
        t = f"chat number {i}"
        table[t] = {"code": 0.2, "writing": 0.1, "quick": 0.3}
        lines.append(f"{t},quick")
    return table, write_csv(tmp_path / name, "\n".join(lines) + "\n")


def sweep_set(tmp_path):
    return semantic_set(tmp_path, 20, 10)


def test_the_split_is_two_exact_halves_whatever_the_order():
    for n in (20, 21, 100, 101):
        rows = [Row(i + 2, f"prompt {i}", "quick") for i in range(n)]
        pick, report = split(rows)
        assert len(pick) == math.ceil(n / 2) and len(report) == n // 2 and len(pick) + len(report) == n
        shuffled = rows[:]
        random.Random(n).shuffle(shuffled)
        again = split(shuffled)
        assert [r.prompt for r in again[0]] == [r.prompt for r in pick] and [r.prompt for r in again[1]] == [r.prompt for r in report]
    digest = lambda r: hashlib.sha256(r.prompt.encode()).digest()  # noqa: E731
    assert max(map(digest, pick)) < min(map(digest, report))  # the cut is by hash of the prompt
    assert [r.prompt for r in split(rows)[0]][:3] == [r.prompt for r in sorted(rows, key=digest)][:3]


def test_the_sweep_picks_on_one_half_reports_on_the_other_and_never_touches_the_config(tmp_path, capsys):
    table, labels = sweep_set(tmp_path)
    config_file = write_csv(tmp_path / "config.yaml", yaml.safe_dump(raw()))  # written once, never rewritten
    before = config_file.read_bytes()
    cfg = cfg_of()
    rows = read_labels(labels, cfg.routes)
    ev = asyncio.run(evalroutes.evaluate(cfg, rows, FakeEmbedder(table)))
    sw = ev.sweep
    assert sw.skipped is None and (sw.n_pick, sw.n_report) == (15, 15)
    assert sw.current == 0.55 and sw.picked.value == 0.45  # 0.40 and 0.45 tie on the pick half: the closer to 0.55 wins
    assert sw.only_current == 0 and sw.only_picked >= 6 and sw.proposes and sw.p < 0.05
    assert cfg.embeddings.threshold == 0.55  # the config copy used for the sweep is a copy

    args = args_for(labels, tmp_path)
    args.config = str(config_file)
    assert evalroutes.run(cfg, args, embedder=FakeEmbedder(table)) == 0
    out = flat(capsys.readouterr().out)  # a long path goes on its own line, and temporary folders differ in length
    assert f"Proposal written to {tmp_path / 'notebook.jsonl'}." in out and "Nothing was applied to your config." in out
    assert "Exact sign test on those rows, two-sided: p = 0.00" in out and "unlikely to be chance" in out
    assert config_file.read_bytes() == before
    line = json.loads((tmp_path / "notebook.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert line["system"] == "trioditis" and line["decision"] == "proposal" and "0.55 -> 0.45" in line["finding"]
    assert not (tmp_path / "home").exists()  # the default notebook was not used

    evalroutes.run(cfg, args, embedder=FakeEmbedder(table))  # the same labels again: no second line
    assert "already in" in capsys.readouterr().out
    assert len((tmp_path / "notebook.jsonl").read_text(encoding="utf-8").splitlines()) == 1


def test_a_threshold_off_the_grid_is_shown_and_ties_use_whole_hundredths(tmp_path):
    table, labels = sweep_set(tmp_path)
    cfg = cfg_of(embeddings={**RAW["embeddings"], "threshold": 0.57})
    ev = asyncio.run(evalroutes.evaluate(cfg, read_labels(labels, cfg.routes), FakeEmbedder(table)))
    values = [c.value for c in ev.sweep.candidates]
    assert values == [0.40, 0.45, 0.50, 0.55, 0.57, 0.60, 0.65, 0.70, 0.75, 0.80]
    assert [c.current for c in ev.sweep.candidates].count(True) == 1 and ev.sweep.candidates[4].current
    assert ev.sweep.picked.value == 0.45  # 0.40 and 0.45 tie; 45 is nearer 57 than 40 is
    text = "\n".join(evalroutes._sweep_lines(ev.sweep))
    assert "0.57" in text and "<- current" in text and "<- picked" in text
    # With nothing to choose between, the current value wins every tie, on or off the grid.
    flat = {t: {"code": 0.1, "writing": 0.1, "quick": 0.9} for t in table}
    for cur in (0.55, 0.57, 0.40):
        cfg = cfg_of(embeddings={**RAW["embeddings"], "threshold": cur})
        ev = asyncio.run(evalroutes.evaluate(cfg, read_labels(labels, cfg.routes), FakeEmbedder(flat)))
        assert ev.sweep.same and ev.sweep.picked.value == cur


def test_the_sweep_is_skipped_with_one_plain_line_when_it_cannot_mean_anything(tmp_path, capsys):
    p = write_csv(tmp_path / "few.csv", "prompt,expected_route\n" + "".join(f"hello number {i},quick\n" for i in range(19)))
    evalroutes.run(cfg_of(), args_for(p, tmp_path), embedder=FakeEmbedder())
    out = capsys.readouterr().out
    assert "skipped: only 19 labelled prompts; the sweep needs at least 20" in out
    assert "the sweep did not run" in out and not (tmp_path / "notebook.jsonl").exists()
    assert "Keywords + embeddings" in out and "accuracy" in out  # the two rows are still graded

    one = raw(routes={"quick": RAW["routes"]["quick"]})
    table, labels = sweep_set(tmp_path)
    one_cfg = parse_config(one)
    lines = "prompt,expected_route\n" + "".join(f"hello number {i},quick\n" for i in range(30))
    evalroutes.run(one_cfg, args_for(write_csv(tmp_path / "q.csv", lines), tmp_path), embedder=FakeEmbedder())
    out = capsys.readouterr().out
    assert "skipped: the config has one route" in out and not (tmp_path / "notebook.jsonl").exists()


def sweep_of(current_ok, picked_ok, picked=0.50):
    pick = Candidate(round(picked * 100), picked, False, 9)
    return Sweep(0.55, None, 10, len(current_ok), [Candidate(55, 0.55, True, 8), pick], pick, current_ok, picked_ok)


def evaluation_of(sw):
    return Evaluation(keywords=Score("Keywords only", ["quick"], ["quick"]), sweep=sw)


def test_a_lopsided_difference_is_called_significant_and_is_recorded(tmp_path):
    sw = sweep_of([True] * 4 + [False] * 8, [True] * 12)  # the picked setting alone is right on 8 rows, the current on none
    text = "\n".join(evalroutes._sweep_lines(sw))
    assert (sw.only_current, sw.only_picked) == (0, 8) and sw.p == pytest.approx(2 / 256) and sw.proposes
    assert "disagree on 8 of the 12 report-half rows: current 0.55 alone got 0 right, picked 0.50 alone got 8 right" in text
    assert "Exact sign test on those rows, two-sided: p = 0.008." in text and "unlikely to be chance" in text
    assert "could be chance" not in text and "4/12 = 33.3%" in text and "12/12 = 100.0%" in text  # each setting keeps its interval
    book = tmp_path / "book.jsonl"
    msg = evalroutes.propose(cfg_of(), evaluation_of(sw), [Row(2, "a", "quick")], "labels.csv", book, False)
    assert msg.startswith("Proposal written") and "p = 0.008" in json.loads(book.read_text(encoding="utf-8"))["finding"]


def test_a_difference_that_could_be_chance_is_said_so_and_not_recorded(tmp_path):
    sw = sweep_of([True] * 9 + [False] * 3, [True] * 12)  # higher on the report half, but only 3 rows differ: p = 0.25
    text = "\n".join(evalroutes._sweep_lines(sw))
    assert sw.better and sw.p == pytest.approx(0.25) and not sw.proposes
    assert "p = 0.250." in text and "p >= 0.05: the difference could be chance." in text and "unlikely" not in text
    book = tmp_path / "book.jsonl"
    msg = evalroutes.propose(cfg_of(), evaluation_of(sw), [Row(2, "a", "quick")], "labels.csv", book, False)
    assert "could be chance (p = 0.250" in msg and msg.startswith("Nothing written") and not book.exists()


def test_the_proposal_needs_p_below_005_exactly(tmp_path):
    five = sweep_of([True] * 5 + [False] * 5, [True] * 10)    # five straight disagreements: p = 0.0625
    six = sweep_of([True] * 4 + [False] * 6, [True] * 10)     # six: p = 0.03125
    assert (five.p, six.p) == (pytest.approx(0.0625), pytest.approx(0.03125))
    assert not five.proposes and six.proposes


def test_a_picked_threshold_that_scored_lower_or_the_same_is_never_proposed(tmp_path):
    worse = sweep_of([True] * 12, [True] * 4 + [False] * 8)   # significantly worse: still no proposal
    same = sweep_of([True] * 6 + [False] * 6, [False] * 6 + [True] * 6)  # same score, different rows: p = 1
    agree = sweep_of([True] * 6 + [False] * 6, [True] * 6 + [False] * 6)
    book = tmp_path / "book.jsonl"
    for sw, part in ((worse, "did not score higher"), (same, "did not score higher"), (agree, "did not score higher")):
        assert not sw.proposes
        assert part in evalroutes.propose(cfg_of(), evaluation_of(sw), [Row(2, "a", "quick")], "l.csv", book, False)
    assert not book.exists()
    assert "agree on every row" in "\n".join(evalroutes._sweep_lines(agree))


def test_dry_run_and_a_set_the_current_threshold_already_handles_write_no_notebook(tmp_path, capsys):
    table, labels = sweep_set(tmp_path)
    cfg = cfg_of()
    args = args_for(labels, tmp_path)
    args.dry_run = True
    evalroutes.run(cfg, args, embedder=FakeEmbedder(table))
    assert "Dry run: would add a proposal" in capsys.readouterr().out and not (tmp_path / "notebook.jsonl").exists()
    flat_table, flat_labels = semantic_set(tmp_path, 0, 30, name="flat.csv")  # every threshold is right everywhere
    evalroutes.run(cfg, args_for(flat_labels, tmp_path), embedder=FakeEmbedder(flat_table))
    assert "already the best on the pick half" in capsys.readouterr().out and not (tmp_path / "notebook.jsonl").exists()


def test_reordering_the_csv_does_not_make_a_second_proposal_but_changed_examples_do(tmp_path, capsys):
    table, labels = sweep_set(tmp_path)
    cfg = cfg_of()
    args = args_for(labels, tmp_path)
    evalroutes.run(cfg, args, embedder=FakeEmbedder(table))
    assert "Proposal written" in capsys.readouterr().out
    lines = labels.read_text(encoding="utf-8").splitlines()
    shuffled = [lines[0], *random.Random(1).sample(lines[1:], len(lines) - 1)]
    assert shuffled != lines
    evalroutes.run(cfg, args_for(write_csv(tmp_path / "shuffled.csv", "\n".join(shuffled) + "\n"), tmp_path), embedder=FakeEmbedder(table))
    assert "already in" in capsys.readouterr().out
    other = raw()
    other["routes"]["quick"]["examples"] = ["a different quick example"]
    evalroutes.run(parse_config(other), args, embedder=FakeEmbedder(table))
    assert "Proposal written" in capsys.readouterr().out
    keys = [json.loads(x)["key"] for x in (tmp_path / "notebook.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(keys) == 2 == len(set(keys))
    # The embeddings model and a threshold are part of the key too.
    rows = read_labels(labels, cfg.routes)
    ev = asyncio.run(evalroutes.evaluate(cfg, rows, FakeEmbedder(table)))
    model = parse_config(raw(embeddings={**RAW["embeddings"], "model": "other-embed"}))
    assert evalroutes.proposal_key(cfg, rows, ev.sweep) != evalroutes.proposal_key(model, rows, ev.sweep)
    assert evalroutes.proposal_key(cfg, rows, ev.sweep) == evalroutes.proposal_key(cfg, rows[::-1], ev.sweep)


def test_a_notebook_with_lines_that_are_not_ours_does_not_break_the_dedupe(tmp_path, capsys):
    table, labels = sweep_set(tmp_path)
    book = tmp_path / "notebook.jsonl"
    junk = '[1]\nnot json at all\n"a string"\n42\nnull\n{"key": 5}\n{"no": "key"}\n\n'
    book.write_text(junk, encoding="utf-8", newline="")
    evalroutes.run(cfg_of(), args_for(labels, tmp_path), embedder=FakeEmbedder(table))
    assert "Proposal written" in capsys.readouterr().out
    text = book.read_text(encoding="utf-8")
    assert text.startswith(junk) and len(text.splitlines()) == len(junk.splitlines()) + 1  # nothing lost, one line added
    evalroutes.run(cfg_of(), args_for(labels, tmp_path), embedder=FakeEmbedder(table))
    assert "already in" in capsys.readouterr().out


def test_strict_csv_header_names_and_short_rows(tmp_path):
    p = write_csv(tmp_path / "l.csv", " Prompt , Expected Route\nhello,quick\nbye,code\n")  # case and spaces do not matter
    assert [r.expected for r in read_labels(p, ROUTES)] == ["quick", "code"]
    p = write_csv(tmp_path / "m.csv", "PROMPT,expected-route\nhello,quick\n")
    assert read_labels(p, ROUTES)[0].prompt == "hello"
    with pytest.raises(LabelsError, match=r"row 3: a quote is opened and never closed"):
        read_labels(write_csv(tmp_path / "q.csv", 'prompt,expected_route\nok,quick\n"never closed,code\n'), ROUTES)


# ------------------------------------------------------------------ determinism and the command line

def test_two_runs_on_the_same_inputs_print_the_same_text(tmp_path, capsys):
    table, labels = sweep_set(tmp_path)
    outs = []
    for _ in range(2):
        evalroutes.run(cfg_of(), args_for(labels, tmp_path), embedder=FakeEmbedder(table), today=lambda: "2026-11-13")
        outs.append(capsys.readouterr().out)
    # Everything but the last paragraph is identical; it differs because the first run recorded the proposal
    # and the second found it already there.
    (body0, last0), (body1, last1) = (o.rsplit("\n\n", 1) for o in outs)
    assert body0 == body1
    assert last0.startswith("Proposal written") and last1.startswith("Nothing written")
    third = []
    for _ in range(2):
        evalroutes.run(cfg_of(), args_for(labels, tmp_path), embedder=FakeEmbedder(table), today=lambda: "2026-11-13")
        third.append(capsys.readouterr().out)
    assert third[0] == third[1] == outs[1]


def config_on_disk(tmp_path, **over):
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(raw(**over)), encoding="utf-8")
    return p


def test_the_command_line_runs_with_the_config_before_or_after_the_command(tmp_path, capsys, monkeypatch):
    table, labels = sweep_set(tmp_path)
    monkeypatch.setattr(evalroutes, "Embedder", lambda cfg, client: FakeEmbedder(table))  # no network, ever
    config = config_on_disk(tmp_path)
    nb = str(tmp_path / "nb.jsonl")
    for argv in (["--config", str(config), "eval-routes", "--labels", str(labels), "--notebook", nb],
                 ["eval-routes", "--labels", str(labels), "--config", str(config), "--notebook", nb, "--dry-run"]):
        with pytest.raises(SystemExit) as stop:
            main(argv)
        out = capsys.readouterr().out
        assert stop.value.code == 0 and "n = 30 labelled prompts" in out and "Keywords + embeddings" in out
        assert f"config: {config}" in flat(out)  # a long path goes on its own line
    assert (tmp_path / "nb.jsonl").exists()  # the first run, not the dry one


def test_the_command_line_survives_an_unreachable_embeddings_provider(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(evalroutes, "Embedder", lambda cfg, client: Down())
    with pytest.raises(SystemExit) as stop:
        main(["eval-routes", "--labels", str(hand_csv(tmp_path)), "--config", str(config_on_disk(tmp_path))])
    out = capsys.readouterr().out
    assert stop.value.code == 0 and "Keywords only" in out and "did not answer" in out


def test_label_errors_leave_the_command_line_with_one_message_and_a_nonzero_exit(tmp_path, capsys):
    config = config_on_disk(tmp_path)
    for labels, part in ((tmp_path / "missing.csv", "labels file not found"),
                         (write_csv(tmp_path / "e.csv", ""), "is empty"),
                         (write_csv(tmp_path / "b.csv", "prompt,expected_route\nhi,nope\n"), "'nope' is not a route")):
        with pytest.raises(SystemExit) as stop:
            main(["--config", str(config), "eval-routes", "--labels", str(labels)])
        assert isinstance(stop.value.code, str) and stop.value.code.startswith("eval-routes: ") and part in stop.value.code
        assert "\n" not in stop.value.code
        assert capsys.readouterr().out == ""


def test_labels_are_required_and_help_goes_to_stdout(tmp_path, capsys):
    with pytest.raises(SystemExit) as stop:
        main(["eval-routes"])
    assert stop.value.code == 2 and "--labels" in capsys.readouterr().err
    with pytest.raises(SystemExit) as stop:
        main(["eval-routes", "--help"])
    out = capsys.readouterr()
    assert stop.value.code == 0 and "--labels" in out.out and "local embeddings provider" in out.out and out.err == ""


# ------------------------------------------------------------------ strata, the baseline, a fixed comparison

def test_a_stratum_column_is_scored_on_its_own_and_a_file_without_one_prints_no_such_block(tmp_path):
    labels = write_csv(tmp_path / "s.csv", "prompt,expected_route,stratum\n"
                       "fix my python script,code,plain\n"         # keyword: right
                       "hello there,quick,plain\n"                 # default: right
                       "make the loop faster,code,indirect\n"      # no keyword: wrong
                       "a note to my landlord,writing,indirect\n"  # no keyword: wrong
                       "python tips,writing,\n")                   # no stratum: counted in the total only
    cfg = cfg_of(embeddings=None)
    rows = read_labels(labels, cfg.routes)
    assert [r.stratum for r in rows] == ["plain", "plain", "indirect", "indirect", ""]
    ev = asyncio.run(evalroutes.evaluate(cfg, rows, None))
    assert ev.keywords.stratum_names() == ["indirect", "plain"]
    assert ev.keywords.per_stratum("plain") == (2, 2) and ev.keywords.per_stratum("indirect") == (0, 2)
    text = "\n".join(evalroutes.format_report(cfg, ev, "s.csv", len(rows), "config.yaml", "2026-11-13"))
    assert "per stratum" in text and "plain     2/2 = 100.0%" in text and "indirect  0/2 = 0.0%" in text
    cfg, rows, ev = graded(tmp_path)
    assert "per stratum" not in "\n".join(evalroutes.format_report(cfg, ev, "l.csv", len(rows), "c.yaml", "2026-11-13"))


def test_the_baseline_is_the_share_of_rows_labelled_with_the_default_route(tmp_path):
    cfg, rows, ev = graded(tmp_path)
    lines = evalroutes.format_report(cfg, ev, "l.csv", len(rows), "c.yaml", "2026-11-13")
    at = lines.index("Baseline: always the default route (quick)")
    assert lines[at + 1].startswith("  accuracy 1/6 = 16.7%")   # one of the six hand-made rows is labelled quick
    assert at < lines.index("Keywords only")


def test_a_named_threshold_is_compared_on_every_row_and_replaces_the_sweep(tmp_path, capsys):
    table, labels = sweep_set(tmp_path)   # 20 code prompts right only at 0.48 or lower, 10 quick ones always right
    emb = FakeEmbedder(table)
    assert evalroutes.run(cfg_of(), args_for(labels, tmp_path, compare_threshold=0.45), embedder=emb) == 0
    out = capsys.readouterr().out
    assert "Threshold comparison" in out and "Threshold sweep" not in out
    assert "current 0.55: 10/30 = 33.3%" in out and "other   0.45: 30/30 = 100.0%" in out
    assert "disagree on 20 of the 30 rows: current 0.55 alone got 0 right, other 0.45 alone got 20 right" in " ".join(out.split())
    assert "p < 0.001" in out and "unlikely to be chance" in out
    assert len(emb.calls) == 30                              # one question per prompt, both thresholds from memory
    assert not (tmp_path / "notebook.jsonl").exists()        # a comparison proposes nothing


def test_a_comparison_that_could_be_chance_says_so():
    c = evalroutes.Compare(0.55, 0.45, [True, False, False, True], [False, True, True, True])
    assert (c.only_current, c.only_other) == (1, 2) and c.p == 1.0
    assert any("could be chance" in line for line in evalroutes._compare_lines(c))
    same = evalroutes.Compare(0.55, 0.45, [True, False], [True, False])
    assert any("cannot tell them apart" in line for line in evalroutes._compare_lines(same))


@pytest.mark.parametrize("value, over, parts", [
    (0.45, {"embeddings": None}, ["--compare-threshold", "embeddings:", "config.example.yaml"]),
    (1.5, {}, ["--compare-threshold 1.5", "0 to 1", "0.55"]),
    (0.55, {}, ["0.55", "config's own", "different value"]),
])
def test_compare_threshold_errors_name_the_cause_and_the_fix(tmp_path, value, over, parts):
    with pytest.raises(SystemExit) as err:
        evalroutes.run(cfg_of(**over), args_for(hand_csv(tmp_path), tmp_path, compare_threshold=value))
    assert all(part in str(err.value) for part in parts)


def test_a_comparison_with_the_embedder_down_says_it_was_not_run(tmp_path, capsys):
    assert evalroutes.run(cfg_of(), args_for(hand_csv(tmp_path), tmp_path, compare_threshold=0.45), embedder=Down()) == 0
    out = capsys.readouterr().out
    assert "Threshold comparison\n  not run" in out and "did not answer" in out


def test_the_shipped_label_set_is_valid_and_balanced():
    """tests/fixtures/routes.csv: 100 prompts, 25 per route of the example config, no duplicates."""
    import collections
    import csv

    from trioditis.evalroutes import read_labels
    repo = Path(__file__).resolve().parents[1]
    routes = list(yaml.safe_load((repo / "config.example.yaml").read_text(encoding="utf-8"))["routes"])
    path = repo / "tests" / "fixtures" / "routes.csv"
    assert len(read_labels(path, routes)) == 100  # read_labels rejects duplicates and unknown routes
    with open(path, encoding="utf-8", newline="") as f:
        per_route = collections.Counter(r["expected_route"] for r in csv.DictReader(f))
    assert per_route == {name: 25 for name in routes}

