"""scripts/stats_walkthrough.py: the worked example of the paired statistics on the public development set.

The expected numbers were computed on 25 Nov 2026 with the functions already in `trioditis_metrics.stats`, and
the counts are re-derived here straight from the two committed CSV files, without the script or the router."""

from __future__ import annotations

import ast
import csv
import re
import shutil
import socket
import sys
from collections import Counter
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import stats_walkthrough as walk  # noqa: E402

SCRIPT = REPO / "scripts" / "stats_walkthrough.py"


@pytest.fixture(scope="module")
def lines() -> list[str]:
    return walk.build()


def test_it_prints_the_numbers_worked_out_beforehand(lines):
    """Each picker's right of 100 with its Wilson interval, the 2x2 table, McNemar, the paired difference and
    the power at the observed 48 disagreements, as computed on 25 Nov 2026."""
    expected = [
        "  keywords     40/100 = 40.0% (30.9% to 49.8%)",
        "  llama3.2:3b  48/100 = 48.0% (38.5% to 57.7%)",
        "  keywords right                     20                  20",
        "  keywords wrong                     28                  32",
        "3. McNemar's test on the 48 prompts where they disagree (only keywords right 20, only llama3.2:3b right 28)",
        "  exact two-sided p  0.312",
        "  mid-p              0.253",
        "  chi-square         1.333, p 0.248",
        "  with correction    1.021, p 0.312",
        "  -8.0 points (-21.2 to +5.5)",
        "  a 65:35 split  0.470",
        "  a 60:40 split  0.215",
        "  a 65:35 split  90; 97",
        "  a 60:40 split  199; 210",
    ]
    for line in expected:
        assert line in lines
    # the order the docstring promises: the rates, the table, the test, the interval, power, per route, sample
    heads = [s.split(".")[0] for s in lines if re.match(r"\d\. ", s)]
    assert heads == ["1", "2", "3", "4", "5", "6", "7"]


def test_an_independent_recount_from_the_raw_files_agrees():
    """llama3.2:3b's right of 100 and per route, counted with csv alone: 48, and 9/18/13/8 on code, reasoning,
    writing and quick. The 2x2 table's llama column (20 + 28) must give the same 48."""
    with (REPO / "tests" / "fixtures" / "routes.csv").open(encoding="utf-8", newline="") as fh:
        labels = [(d["prompt"].strip(), d["expected_route"].strip()) for d in csv.DictReader(fh)]
    with walk.BASELINE.open(encoding="utf-8", newline="") as fh:
        saved = {d["prompt"].strip(): d["model_route"] for d in csv.DictReader(fh)}
    assert len(labels) == 100
    assert Counter(route for _, route in labels) == {"code": 25, "reasoning": 25, "writing": 25, "quick": 25}
    right = Counter(route for prompt, route in labels if saved[prompt] == route)
    assert sum(right.values()) == 48
    assert right == {"code": 9, "reasoning": 18, "writing": 13, "quick": 8}


def test_the_per_route_lines_match_the_recount(lines):
    """The llama3.2:3b column of section 6 holds the recounted 9/18/13/8; the keyword column adds up to 40."""
    keywords = 0
    for route, k in (("code", 9), ("reasoning", 18), ("writing", 13), ("quick", 8)):
        row = next(s for s in lines if s.startswith(f"  {route} "))
        first, second = row.split("%)", 1)
        assert second.strip().startswith(f"{k}/25 = ")
        keywords += int(first.split()[1].split("/")[0])
    assert keywords == 40


def test_the_keyword_rate_is_the_one_published_in_route_accuracy(lines):
    """docs/route-accuracy.md, "What is already known", prints "| Keywords only | 40/100 | 30.9–49.8% |"."""
    page = (REPO / "docs" / "route-accuracy.md").read_text(encoding="utf-8")
    found = re.search(r"^\| Keywords only \| (\d+)/(\d+) \| ([\d.]+)–([\d.]+)% \|$", page, re.MULTILINE)
    assert found, "the Keywords only row of the table in docs/route-accuracy.md moved or changed format"
    k, n, lo, hi = found.groups()
    assert f"  keywords     {k}/{n} = {100 * int(k) / int(n):.1f}% ({lo}% to {hi}%)" in lines


def test_the_stratified_sample_takes_five_of_each_route(lines):
    assert "  right in the sample  8/20" in lines
    assert "  the interval holds the full-set value" in lines


def test_main_prints_the_lines_and_exits_0(capsys):
    assert walk.main([]) == 0
    assert capsys.readouterr().out.splitlines() == walk.build()


def test_no_socket_is_opened(monkeypatch):
    """The keyword router runs with no embedder and the model's picks come from a file, so nothing connects."""
    def refuse(*args, **kwargs):
        raise AssertionError("stats_walkthrough opened a network connection")

    # asyncio's event loop wakes itself through socket.socketpair(); on Windows that pair is two sockets in this
    # process joined by connect(), so the pair is built with the real connect and every other connect is refused.
    real_connect, real_socketpair = socket.socket.connect, socket.socketpair

    def socketpair(*args, **kwargs):
        with monkeypatch.context() as m:
            m.setattr(socket.socket, "connect", real_connect)
            return real_socketpair(*args, **kwargs)

    monkeypatch.setattr(socket, "socketpair", socketpair)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    assert walk.build()[5] == "  keywords     40/100 = 40.0% (30.9% to 49.8%)"


def test_the_script_imports_no_network_library():
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    names = {a.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for a in node.names}
    names |= {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    assert not names & {"http", "httpx", "requests", "urllib", "urllib3", "socket", "aiohttp", "openai"}


def test_a_config_with_a_route_picker_is_refused(tmp_path, capsys, monkeypatch):
    """A route_picker would ask a model for every prompt; the example must never do that."""
    config = tmp_path / "config.yaml"
    config.write_text(walk.CONFIG.read_text(encoding="utf-8")
                      + "\nroute_picker:\n  provider: ollama\n  model: qwen2.5-coder:7b\n", encoding="utf-8")
    with pytest.raises(walk.Refused, match="route_picker"):
        walk.build(config=config)
    monkeypatch.setattr(walk.build, "__defaults__", (walk.LABELS, config, walk.BASELINE))
    assert walk.main([]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err.startswith("stats_walkthrough: the config has a route_picker")


def test_a_baseline_missing_a_prompt_is_refused(tmp_path):
    with walk.BASELINE.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    with (REPO / "tests" / "fixtures" / "routes.csv").open(encoding="utf-8", newline="") as fh:
        first_label = next(csv.DictReader(fh))["prompt"].strip()
    kept = [r for r in rows if r["prompt"].strip() != first_label]
    assert len(kept) == len(rows) - 1
    path = tmp_path / "baseline.csv"
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["prompt", "model_route"])
        writer.writeheader()
        writer.writerows(kept)
    with pytest.raises(walk.Refused, match=r"no saved pick for 1 of the 100 prompts \(label rows \[2\]"):
        walk.build(baseline=path)


def test_a_saved_pick_that_is_not_a_route_is_refused(tmp_path):
    with walk.BASELINE.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    with (REPO / "tests" / "fixtures" / "routes.csv").open(encoding="utf-8", newline="") as fh:
        first_label = next(csv.DictReader(fh))["prompt"].strip()
    for r in rows:
        if r["prompt"].strip() == first_label:
            r["model_route"] = "poetry"
    path = tmp_path / "baseline.csv"
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["prompt", "model_route"])
        writer.writeheader()
        writer.writerows(rows)
    with pytest.raises(walk.Refused, match=r"not routes of the config: \['poetry'\]"):
        walk.build(baseline=path)


def test_a_baseline_without_the_columns_is_refused(tmp_path):
    path = tmp_path / "baseline.csv"
    shutil.copy(REPO / "tests" / "fixtures" / "routes.csv", path)
    with pytest.raises(walk.Refused, match="prompt and model_route columns"):
        walk.build(baseline=path)


# ---------------------------------------------------------------- the page that teaches it

PAGE = REPO / "docs" / "eval-statistics.md"


def test_the_page_holds_the_scripts_output_word_for_word(lines):
    """docs/eval-statistics.md shows the full output in the one ```text block under "## The worked example";
    when the script or the data change, this fails until the page is updated."""
    page = PAGE.read_text(encoding="utf-8")
    section = page.split("\n## The worked example\n", 1)[1].split("\n## ", 1)[0]
    blocks = re.findall(r"^```text\n(.*?)\n```$", section, re.MULTILINE | re.DOTALL)
    assert len(blocks) == 1
    assert blocks[0].split("\n") == lines


def test_the_page_has_a_section_for_each_method():
    headings = re.findall(r"^## (.+)$", PAGE.read_text(encoding="utf-8"), re.MULTILINE)
    for name in ("The Wilson interval", "The paired table", "McNemar's test: exact, mid-p and chi-square",
                 "The paired difference interval", "Power, and why it is a saw-tooth", "Disagreements needed",
                 "Stratified sampling and its estimate", "Already in place: Holm and Cohen's kappa",
                 "The worked example", "Limits", "References"):
        assert name in headings


def test_every_function_the_page_names_exists():
    import compare_results

    from trioditis_metrics import stats

    names = set(re.findall(r"`([a-z_]+)\(", PAGE.read_text(encoding="utf-8")))
    assert names >= {"wilson", "paired_counts", "mcnemar", "sign_test", "paired_difference", "sign_test_power",
                     "paired_power", "discordant_needed", "stratum_allocation", "stratified_sample",
                     "stratified_proportion", "cohen_kappa"}
    for name in names:
        assert hasattr(stats, name) or hasattr(compare_results, name), name
