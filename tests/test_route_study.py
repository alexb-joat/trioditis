"""scripts/route_study.py: the helpers behind docs/route-accuracy.md."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import route_study as rs  # noqa: E402

REPO = Path(__file__).resolve().parent.parent


def test_agreement_prints_kappa_per_set_and_who_said_what(tmp_path, capsys):
    f = tmp_path / "l.csv"
    f.write_text("set,prompt,writer_route,second_route\n"
                 "first,a,code,code\nfirst,b,quick,quick\nfirst,c,code,quick\nfirst,d,quick,quick\n"
                 "second,e,code,code\nsecond,f,quick,quick\n", encoding="utf-8")
    assert rs.agreement(SimpleNamespace(file=str(f), a="writer_route", b="second_route", by="set")) == 0
    out = capsys.readouterr().out
    assert "all rows: n = 6" in out and "same label 5/6 = 83.3%" in out
    assert "set = first: n = 4" in out and "Cohen's kappa 0.50" in out   # observed 0.75, chance 0.5
    assert "set = second: n = 2" in out and "Cohen's kappa 1.00" in out
    assert "rows: writer_route; columns: second_route" in out


def test_a_missing_column_is_named(tmp_path):
    f = tmp_path / "l.csv"
    f.write_text("prompt,writer_route\na,code\n", encoding="utf-8")
    with pytest.raises(SystemExit) as err:
        rs.agreement(SimpleNamespace(file=str(f), a="writer_route", b="second_route", by=""))
    assert "second_route" in str(err.value) and "found: prompt, writer_route" in str(err.value)


def test_the_model_baseline_only_talks_to_this_machine(tmp_path):
    args = SimpleNamespace(file="unused.csv", model="m", base_url="https://example.com/v1",
                           config=str(REPO / "config.example.yaml"), label="expected_route", by="", out=None)
    with pytest.raises(SystemExit) as err:
        rs.model_baseline(args)
    assert "not on this machine" in str(err.value)


def test_the_study_record_holds_both_labels_for_every_prompt():
    rows = rs.read(str(REPO / "docs" / "route-accuracy" / "labels.csv"), ["set", "prompt", "writer_route", "second_route"])
    routes = {"code", "reasoning", "writing", "quick"}
    assert all(r["writer_route"] in routes and r["second_route"] in routes for r in rows)
    assert len({r["prompt"] for r in rows}) == len(rows)
    assert sum(r["set"] == "first" for r in rows) == 100
    assert all(r["stratum"] in ("plain", "indirect") for r in rows if r["set"] == "second")
