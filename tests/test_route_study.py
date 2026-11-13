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


STUDY = ("id,set,prompt,stratum,borderline,writer_route,second_route\n"
         "1,first,old one,,no,code,quick\n"
         "2,second,\"fix it, please\",plain,no,code,code\n"
         "3,second,hello,plain,no,quick,quick\n"
         "4,second,readme,indirect,yes,code,writing\n"
         "5,second,riddle,plain,yes,reasoning,quick\n"
         "6,second,which db,indirect,yes,code,reasoning\n")


def heldout_args(tmp_path, settled: str):
    (tmp_path / "l.csv").write_text(STUDY, encoding="utf-8")
    (tmp_path / "s.csv").write_text("id,prompt,decision\n" + settled, encoding="utf-8")
    return SimpleNamespace(file=str(tmp_path / "l.csv"), settled=str(tmp_path / "s.csv"), out=str(tmp_path / "h.csv"),
                           set="second", a="writer_route", b="second_route")


def test_heldout_takes_agreed_labels_and_the_maintainers_decisions(tmp_path, capsys):
    args = heldout_args(tmp_path, "4,readme,Writing\n5,riddle,drop\n6,which db,quick\n")
    assert rs.heldout(args) == 0
    assert (tmp_path / "h.csv").read_text(encoding="utf-8") == (
        "prompt,expected_route,stratum,borderline\n"
        "\"fix it, please\",code,plain,no\nhello,quick,plain,no\nreadme,writing,indirect,yes\nwhich db,quick,indirect,yes\n")
    out = capsys.readouterr().out
    assert "set second: 5 prompts, both labels the same on 2, 3 decided by the maintainer" in out
    assert "with second_route: 1" in out and "dropped: 1" in out and "a third route: 1" in out
    assert "wrote 4 rows" in out and "code 1, quick 2, writing 1" in out


@pytest.mark.parametrize("settled, said", [
    ("4,readme,writing\n5,riddle,\n6,which db,code\n", "row 5 has no decision yet"),
    ("4,readme,writing\n6,which db,code\n", "row 5 has no decision yet"),
    ("4,readme,prose\n5,riddle,quick\n6,which db,code\n", "row 4 in"),
    ("2,fix it,code\n4,readme,writing\n5,riddle,quick\n6,which db,code\n", "decides row 2"),
    ("1,old one,code\n4,readme,writing\n5,riddle,quick\n6,which db,code\n", "decides row 1"),
])
def test_heldout_stops_on_an_open_wrong_or_stray_decision_and_writes_nothing(tmp_path, settled, said):
    args = heldout_args(tmp_path, settled)
    with pytest.raises(SystemExit) as err:
        rs.heldout(args)
    assert said in str(err.value) and not (tmp_path / "h.csv").exists()
