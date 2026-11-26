"""scripts/compare_results.py: the paired comparison of route pickers from their `--answers` files.

Expected values for the hand-made pairs are written here, derived by hand, before the script is run on them."""

from __future__ import annotations

import csv
import json
import math
import random
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import compare_results as cmp  # noqa: E402

from tests.test_measure import Server, run  # noqa: E402
from trioditis_metrics.stats import mcnemar, paired_counts  # noqa: E402

ROWS, FINGERPRINT, ROUTES = cmp.shipped()


def wrong_route(expected: str, i: int) -> str:
    """A route that is not the expected one: the next route in the list, or "-" for every third wrong row."""
    return "-" if i % 3 == 0 else ROUTES[(ROUTES.index(expected) + 1) % len(ROUTES)]


def write(tmp_path: Path, name: str, routes: list[str], picker: str = "model-x", **over) -> Path:
    doc = {"schema": "trioditis-answers.v1", "trioditis": "0.7.0", "heldout_sha256_16": FINGERPRINT, "picker": picker,
           "answers": [{"row": r.number, "route": p} for r, p in zip(ROWS, routes, strict=True)]} | over
    path = tmp_path / name
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def by_rule(tmp_path: Path, name: str, is_right, picker: str = "model-x") -> Path:
    return write(tmp_path, name, [r.expected if is_right(i) else wrong_route(r.expected, i) for i, r in enumerate(ROWS)], picker)


def pair(tmp_path: Path, b: int, c: int, tag: str = "") -> tuple[Path, Path]:
    """Baseline and candidate that disagree on b + c prompts: the first b only the baseline gets right, the next c
    only the candidate; every other prompt both get right."""
    base = by_rule(tmp_path, f"base{tag}.json", lambda i: not (b <= i < b + c), "base")
    cand = by_rule(tmp_path, f"cand{tag}.json", lambda i: not i < b, "cand")
    return base, cand


def load(path: Path) -> cmp.Answers:
    return cmp.load(path, ROWS, FINGERPRINT, ROUTES)


# ---------------------------------------------------------------- the shipped labels

def test_the_shipped_set_has_315_rows_numbered_2_to_316_and_a_fingerprint():
    assert len(ROWS) == 315 and [r.number for r in ROWS] == list(range(2, 317))
    assert len(FINGERPRINT) == 16 and ROUTES == ["code", "reasoning", "writing", "quick"]


# ---------------------------------------------------------------- Holm against an independent definition

def step_down_rejects(ps: list[float], alpha: float) -> set[int]:
    """Holm's procedure as a procedure: sort ascending, reject while p(j) <= alpha / (k - j), stop at the first failure."""
    k, rejected = len(ps), set()
    for rank, i in enumerate(sorted(range(k), key=lambda j: ps[j])):
        if ps[i] <= alpha / (k - rank):
            rejected.add(i)
        else:
            break
    return rejected


def brute_adjusted(ps: list[float], i: int) -> float:
    """The smallest alpha at which the step-down procedure rejects hypothesis i (1 if none up to alpha = 1)."""
    if i not in step_down_rejects(ps, 1.0):
        return 1.0
    lo, hi = 0.0, 1.0
    for _ in range(80):
        mid = (lo + hi) / 2
        lo, hi = (lo, mid) if i in step_down_rejects(ps, mid) else (mid, hi)
    return hi


def test_holm_by_hand():
    # sorted: 0.005 x4 = 0.02, 0.01 x3 = 0.03, 0.03 x2 = 0.06, 0.04 x1 = 0.04 -> running maximum 0.06; back in the given order
    assert cmp.holm([0.01, 0.04, 0.03, 0.005]) == pytest.approx([0.03, 0.06, 0.06, 0.02], abs=1e-12)
    assert cmp.holm([0.2]) == [0.2] and cmp.holm([]) == []
    assert cmp.holm([0.6, 0.7]) == [1.0, 1.0]                      # 0.6 x 2 = 1.2, capped at 1
    assert cmp.holm([0.03, 0.03]) == pytest.approx([0.06, 0.06])   # a tie: both get the larger product, 2 x 0.03


def test_holm_agrees_with_the_step_down_procedure_on_random_p_values():
    rng = random.Random(20261125)
    for _ in range(300):
        k = rng.randint(1, 9)
        ps = [rng.choice([0.0, 1.0, round(rng.random(), 2), rng.random() ** 3]) for _ in range(k)]
        got = cmp.holm(ps)
        for i in range(k):
            assert got[i] == pytest.approx(brute_adjusted(ps, i), abs=1e-9), (ps, i)
        assert all(0.0 <= g <= 1.0 for g in got) and all(g >= p - 1e-12 for g, p in zip(got, ps, strict=True))


def test_the_independent_check_can_fail():
    ps = [0.01, 0.04, 0.03, 0.005]
    assert ps != pytest.approx([brute_adjusted(ps, i) for i in range(4)], abs=1e-9)             # unadjusted p is not Holm
    assert [min(1.0, 4 * p) for p in ps] != pytest.approx([brute_adjusted(ps, i) for i in range(4)], abs=1e-9)   # nor is Bonferroni


# ---------------------------------------------------------------- the numbers on a hand-made pair

def test_a_hand_made_pair_gives_the_counts_and_the_sign_test_worked_out_by_hand(tmp_path, capsys):
    """Row i (0 to 314): the baseline is right unless i is a multiple of 3, the candidate unless i is a multiple of 5.
    Multiples: of 3 -> 105, of 5 -> 63, of 15 -> 21. So the baseline is right on 315 - 105 = 210, the candidate on
    315 - 63 = 252; only the baseline right: multiples of 5 that are not of 3 = 63 - 21 = 42; only the candidate:
    105 - 21 = 84."""
    base = by_rule(tmp_path, "base.json", lambda i: i % 3 != 0, "base-model")
    cand = by_rule(tmp_path, "cand.json", lambda i: i % 5 != 0, "cand-model")
    [row] = cmp.compare(load(base), [load(cand)], ROWS)
    assert (row["right"], row["n"], row["only_baseline"], row["only_candidate"], row["ahead"]) == (252, 315, 42, 84, True)
    n = 42 + 84
    expected_p = float(Fraction(2 * sum(math.comb(n, i) for i in range(42 + 1)), 2 ** n))     # two-sided exact binomial
    assert row["p"] == pytest.approx(expected_p, rel=1e-12) and 1e-4 < expected_p < 1e-3         # a sanity band, not the value
    assert row["p_holm"] == row["p"]                                  # k = 1: no adjustment
    assert row["verdict"] == "ahead and significant"
    z, p = 1.96, 252 / 315                                            # Wilson, written out again
    centre = (p + z * z / (2 * 315)) / (1 + z * z / 315)
    half = z * math.sqrt(p * (1 - p) / 315 + z * z / (4 * 315 ** 2)) / (1 + z * z / 315)
    assert (row["low95"], row["high95"]) == pytest.approx((centre - half, centre + half), abs=2e-4)
    assert cmp.main(["compare", "--baseline", str(base), str(cand)]) == 0
    out = capsys.readouterr().out
    assert "Baseline base-model (base.json): 210/315 right = 66.7%" in out
    assert "cand-model (cand.json): 252/315 right = 80.0%" in out
    assert "only the baseline right 42, only this one right 84" in out and "ahead and significant" in out


# ---------------------------------------------------------------- what counts as "ahead and significant"

def test_p_just_under_0_05_alone_is_significant_and_not_with_a_second_comparison(tmp_path):
    b, c = 8, 20                                                       # raw exact p = 0.0357 (checked below)
    base, cand = pair(tmp_path, b, c)
    [alone] = cmp.compare(load(base), [load(cand)], ROWS)
    assert 0.025 < alone["p"] < 0.05 and alone["verdict"] == "ahead and significant"
    two = cmp.compare(load(base), [load(cand), load(cand)], ROWS)       # two candidates, each with raw p 0.0357
    assert [r["p"] for r in two] == [alone["p"]] * 2
    assert [r["p_holm"] for r in two] == pytest.approx([2 * alone["p"]] * 2) and 2 * alone["p"] >= 0.05
    assert [r["verdict"] for r in two] == ["not shown to differ"] * 2


def test_behind_is_never_called_ahead_and_a_tie_in_counts_is_not_ahead(tmp_path):
    base, cand = pair(tmp_path, 60, 5)                                  # the candidate loses on 60 prompts, wins on 5
    [behind] = cmp.compare(load(base), [load(cand)], ROWS)
    assert behind["only_baseline"] == 60 and behind["only_candidate"] == 5
    assert not behind["ahead"] and behind["p_holm"] < 0.05 and behind["verdict"] == "behind and significant"
    base, cand = pair(tmp_path, 12, 12, "-tie")                         # equal counts right: p = 1
    [tie] = cmp.compare(load(base), [load(cand)], ROWS)
    assert tie["right"] == sum(cmp.right(ROWS, load(base))) and tie["p"] == 1.0 and not tie["ahead"]
    assert tie["verdict"] == "not shown to differ"


def test_identical_answers_never_differ(tmp_path):
    same = by_rule(tmp_path, "same.json", lambda i: i % 4 != 0)
    [row] = cmp.compare(load(same), [load(same)], ROWS)
    assert (row["only_baseline"], row["only_candidate"], row["p"], row["verdict"]) == (0, 0, 1.0, "not shown to differ")


def test_several_candidates_get_holm_over_all_of_them_in_the_order_given(tmp_path):
    base = by_rule(tmp_path, "base.json", lambda i: i % 2 == 0, "base")
    cands = [by_rule(tmp_path, f"c{k}.json", lambda i, k=k: (i % 2 == 0) or i < 10 * (k + 1), f"c{k}") for k in range(3)]
    rows = cmp.compare(load(base), [load(p) for p in cands], ROWS)
    assert [r["name"] for r in rows] == [f"c{k} (c{k}.json)" for k in range(3)]
    assert [r["p_holm"] for r in rows] == pytest.approx(cmp.holm([r["p"] for r in rows]))


# ---------------------------------------------------------------- how many answers changed

def test_changed_counts_prompts_with_a_different_route(tmp_path):
    right = [r.expected for r in ROWS]
    x = write(tmp_path, "x.json", right, "m")
    flipped = list(right)
    flipped[0] = wrong_route(right[0], 1)                  # right -> wrong
    flipped[1] = wrong_route(right[1], 1)                  # right -> wrong
    y = write(tmp_path, "y.json", flipped, "m")
    assert cmp.changed(load(x), load(y), ROWS) == {"n": 315, "changed": 2, "right_only_first": 2, "right_only_second": 0}
    assert cmp.changed(load(y), load(x), ROWS) == {"n": 315, "changed": 2, "right_only_first": 0, "right_only_second": 2}
    both_wrong_x, both_wrong_y = list(right), list(right)
    both_wrong_x[2] = ROUTES[(ROUTES.index(right[2]) + 1) % 4]       # wrong in both runs, but a different wrong route
    both_wrong_y[2] = ROUTES[(ROUTES.index(right[2]) + 2) % 4]
    xx, yy = write(tmp_path, "xx.json", both_wrong_x, "m"), write(tmp_path, "yy.json", both_wrong_y, "m")
    assert cmp.changed(load(xx), load(yy), ROWS) == {"n": 315, "changed": 1, "right_only_first": 0, "right_only_second": 0}
    assert cmp.changed(load(x), load(x), ROWS)["changed"] == 0


def test_changed_says_when_the_two_files_are_two_models(tmp_path, capsys):
    x, y = by_rule(tmp_path, "x.json", lambda i: True, "model-one"), by_rule(tmp_path, "y.json", lambda i: i % 2 == 0, "model-two")
    assert cmp.main(["changed", str(x), str(y)]) == 0
    out = capsys.readouterr().out
    assert "157 of 315 prompts got a different route" in out and "note: these are two different models" in out
    z = by_rule(tmp_path, "z.json", lambda i: True, "model-one")
    assert cmp.main(["changed", str(x), str(z)]) == 0 and "note:" not in capsys.readouterr().out


# ---------------------------------------------------------------- files that are refused

def good_routes() -> list[str]:
    return [r.expected for r in ROWS]


def raw(folder: Path, text: str) -> Path:
    path = folder / "a.json"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize("make, words", [
    (lambda p: write(p, "a.json", good_routes(), schema="trioditis-answers.v2"), "schema is 'trioditis-answers.v2'"),
    (lambda p: write(p, "a.json", good_routes(), heldout_sha256_16="0" * 16), "different held-out set"),
    (lambda p: write(p, "a.json", good_routes(), extra="x"), "not an answers file"),
    (lambda p: write(p, "a.json", good_routes(), answers=[{"row": r.number, "route": r.expected} for r in ROWS[:-1]]), "expected 315 answers"),
    (lambda p: write(p, "a.json", good_routes(), answers=[{"row": 2, "route": "code"}] * 315), "appears twice"),
    (lambda p: write(p, "a.json", good_routes(), answers=[{"row": r.number + 1, "route": r.expected} for r in ROWS]), "not the shipped set's rows"),
    (lambda p: write(p, "a.json", good_routes(), answers=[{"row": r.number, "route": "poetry"} for r in ROWS]), "not a shipped route"),
    (lambda p: write(p, "a.json", good_routes(), answers=[{"row": r.number, "route": r.expected, "prompt": "hi"} for r in ROWS]), "exactly {row, route}"),
    (lambda p: write(p, "a.json", good_routes(), answers=[{"row": str(r.number), "route": r.expected} for r in ROWS]), "whole-number row"),
    (lambda p: write(p, "a.json", good_routes(), picker=""), "must be text"),
    (lambda p: raw(p, "not json"), "not JSON"),
    (lambda p: raw(p, '["a"]'), "not an answers file"),
    (lambda p: raw(p, " " * 2_100_000), "larger than 2,000,000 bytes"),
    (lambda p: p / "missing.json", "cannot read it"),
])
def test_a_file_that_is_not_a_complete_answers_file_for_this_set_is_refused(tmp_path, capsys, make, words):
    path = make(tmp_path)
    good = write(tmp_path, "good.json", good_routes())
    with pytest.raises(cmp.Refused) as e:
        load(path)
    assert words in str(e.value)
    assert cmp.main(["compare", "--baseline", str(good), str(path)]) == 1          # a candidate is refused...
    assert cmp.main(["compare", "--baseline", str(path), str(good)]) == 1          # ...and so is a baseline
    assert cmp.main(["changed", str(path), str(good)]) == 1
    err = capsys.readouterr().err
    assert err.count("compare_results: ") == 3 and "Traceback" not in err


def test_a_complete_file_is_accepted(tmp_path):
    answers = load(write(tmp_path, "a.json", good_routes(), "some-model"))
    assert answers.picker == "some-model" and len(answers.routes) == 315 and answers.name == "some-model (a.json)"


# ---------------------------------------------------------------- the paired table

def table_pair(tmp_path: Path) -> tuple[Path, Path]:
    """The hand-made pair of the test above: row i (0 to 314) is right for the first unless i is a multiple of 3, for
    the second unless i is a multiple of 5. Both right 315 - 42 - 84 - 21 = 168, only the first 42, only the second 84,
    both wrong 21 (the multiples of 15)."""
    return (by_rule(tmp_path, "first.json", lambda i: i % 3 != 0, "first-model"),
            by_rule(tmp_path, "second.json", lambda i: i % 5 != 0, "second-model"))


def test_the_table_has_one_row_per_prompt_and_no_prompt_text(tmp_path, capsys):
    first, second = table_pair(tmp_path)
    out_file = tmp_path / "table.csv"
    assert cmp.main(["table", str(first), str(second), "--out", str(out_file)]) == 0
    text = out_file.read_text(encoding="utf-8")
    with out_file.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        data = list(reader)
    assert tuple(reader.fieldnames) == cmp.TABLE_COLUMNS == ("row", "expected", "stratum", "first_route", "second_route",
                                                            "first_right", "second_right")
    assert len(data) == 315 and [int(d["row"]) for d in data] == [r.number for r in ROWS]
    assert [d["expected"] for d in data] == [r.expected for r in ROWS] and [d["stratum"] for d in data] == [r.stratum for r in ROWS]
    assert sum(int(d["first_right"]) for d in data) == 210 and sum(int(d["second_right"]) for d in data) == 252
    assert all(d["first_right"] in ("0", "1") and d["second_right"] in ("0", "1") for d in data)
    for i, d in enumerate(data):                                       # the 0/1 columns agree with the routes beside them
        assert (d["first_route"] == d["expected"]) == (d["first_right"] == "1") == (i % 3 != 0)
        assert (d["second_route"] == d["expected"]) == (d["second_right"] == "1") == (i % 5 != 0)
    assert not any(r.prompt.strip() in text for r in ROWS)             # like the answers files: no prompt text


def test_the_table_prints_the_paired_counts_mcnemar_and_where_the_verdict_comes_from(tmp_path, capsys):
    first, second = table_pair(tmp_path)
    assert cmp.main(["table", str(first), str(second), "--out", str(tmp_path / "t.csv")]) == 0
    out = capsys.readouterr().out
    assert "first  = first-model (first.json)" in out and "second = second-model (second.json)" in out
    assert "315 held-out prompts" in out
    assert any(line.split() == ["first", "right", "168", "42"] for line in out.splitlines())      # both right, only the first
    assert any(line.split() == ["first", "wrong", "84", "21"] for line in out.splitlines())       # only the second, both wrong
    assert "on the 126 prompts where they disagree (only the first right 42, only the second right 84)" in out
    m = mcnemar(42, 84)
    assert f"exact two-sided p {m['exact_p']:.3g}; mid-p {m['mid_p']:.3g}" in out
    assert f"chi-square {m['chi2']:.3g}, p {m['chi2_p']:.3g}; with continuity correction {m['chi2_cc']:.3g}, p {m['chi2_cc_p']:.3g}" in out
    assert "comes from `compare` (the exact two-sided test with Holm over all candidates), not from this table" in out
    assert f"Wrote 315 rows to {tmp_path / 't.csv'}" in out


def test_the_table_counts_are_the_ones_compare_uses(tmp_path):
    first, second = table_pair(tmp_path)
    counts =paired_counts(cmp.right(ROWS, load(first)), cmp.right(ROWS, load(second)))
    assert counts == {"n": 315, "both_right": 168, "only_first": 42, "only_second": 84, "both_wrong": 21}
    [row] = cmp.compare(load(first), [load(second)], ROWS)
    assert (row["only_baseline"], row["only_candidate"]) == (counts["only_first"], counts["only_second"])


@pytest.mark.parametrize("make, words", [
    (lambda p: write(p, "bad.json", good_routes(), schema="trioditis-answers.v2"), "schema is 'trioditis-answers.v2'"),
    (lambda p: write(p, "bad.json", good_routes(), heldout_sha256_16="0" * 16), "different held-out set"),
    (lambda p: raw(p, "not json"), "not JSON"),
    (lambda p: p / "missing.json", "cannot read it"),
])
def test_a_refused_file_gives_table_the_same_message_and_exit_1_as_compare(tmp_path, capsys, make, words):
    bad, good = make(tmp_path), write(tmp_path, "good.json", good_routes())
    out_file = tmp_path / "table.csv"
    assert cmp.main(["compare", "--baseline", str(bad), str(good)]) == 1
    from_compare = capsys.readouterr().err
    assert cmp.main(["table", str(bad), str(good), "--out", str(out_file)]) == 1
    from_table = capsys.readouterr()
    assert from_table.err == from_compare and words in from_table.err and from_table.out == ""
    assert cmp.main(["table", str(good), str(bad), "--out", str(out_file)]) == 1      # the second file is checked too
    assert words in capsys.readouterr().err
    assert not out_file.exists()                                       # nothing is written for a refused file


def test_the_table_is_never_written_over_an_existing_file_or_into_a_missing_folder(tmp_path, capsys):
    first, second = table_pair(tmp_path)
    existing = tmp_path / "keep.csv"
    existing.write_text("mine\n", encoding="utf-8")
    assert cmp.main(["table", str(first), str(second), "--out", str(existing)]) == 1
    assert "already exists" in capsys.readouterr().err and existing.read_text(encoding="utf-8") == "mine\n"
    assert cmp.main(["table", str(first), str(second), "--out", str(tmp_path / "no-such-folder" / "t.csv")]) == 1
    assert "cannot write it" in capsys.readouterr().err


def test_table_needs_two_files_and_an_output():
    for argv in (["table"], ["table", "a.json", "b.json"], ["table", "--out", "t.csv", "a.json"]):
        with pytest.raises(SystemExit) as e:
            cmp.main(argv)
        assert e.value.code == 2


def test_the_script_does_not_import_pandas():
    text = (REPO / "scripts" / "compare_results.py").read_text(encoding="utf-8")
    assert not any(line.startswith(("import pandas", "from pandas")) for line in text.splitlines())


# ---------------------------------------------------------------- the writer and the reader fit

def test_files_written_by_results_answers_are_read_and_compared(tmp_path):
    first, second = tmp_path / "one", tmp_path / "two"
    first.mkdir(), second.mkdir()
    run(first, Server(), answers=str(first / "a.json"))
    run(second, Server(reply=lambda text: "code"), answers=str(second / "a.json"))
    a, b = load(first / "a.json"), load(second / "a.json")
    assert a.picker == b.picker == "tiny:1b" and len(a.routes) == 315
    [row] = cmp.compare(a, [b], ROWS)
    assert row["n"] == 315 and row["only_baseline"] + row["only_candidate"] <= 315
    results = json.loads((first / "trioditis-results.json").read_text(encoding="utf-8"))
    assert sum(cmp.right(ROWS, a)) == results["heldout"]["picker"]["right"]           # the same count the results file gives
    assert cmp.changed(a, a, ROWS)["changed"] == 0


# ---------------------------------------------------------------- the command line

def test_help_starts_with_the_usage_line():
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "compare_results.py"), "--help"], capture_output=True, text=True,
                       timeout=60, check=False)
    assert r.returncode == 0 and r.stdout.splitlines()[0].startswith("usage: compare_results.py")


def test_compare_needs_a_baseline_and_a_candidate():
    for argv in (["compare"], ["compare", "--baseline", "a.json"], ["compare", "x.json"], []):
        with pytest.raises(SystemExit) as e:
            cmp.main(argv)
        assert e.value.code == 2


def test_the_script_names_no_model():
    text = (REPO / "scripts" / "compare_results.py").read_text(encoding="utf-8")
    for name in ("qwen", "llama", "gemma", "phi", "mistral", "smollm", "deepseek", "tiny:1b", "nomic"):
        assert name not in text.lower()
