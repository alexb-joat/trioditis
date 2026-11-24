"""scripts/collect_results.py: shared results are untrusted data; only checked, consented files become rows."""

import copy
import json
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import collect_results as cr  # noqa: E402

from trioditis import measure  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
GOOD = json.loads((REPO / "tests" / "fixtures" / "results-v1.json").read_text(encoding="utf-8"))
CONSENT = "I understand this issue is public, and I release these numbers under CC0"


def body(doc, ticked=True, results=None):
    """An issue body laid out the way GitHub writes a submitted issue form: one `###` heading per field."""
    text = results if results is not None else json.dumps(doc, indent=2)
    box = "X" if ticked else " "
    return (f"### Results file\n\n```json\n{text}\n```\n\n### Sharing\n\n- [{box}] {CONSENT}\n\n"
            "### Anything unusual about the run? (optional)\n\n_No response_")


def rejected(doc) -> str:
    with pytest.raises(cr.Rejected) as e:
        cr.check(doc)
    return str(e.value)


def test_the_form_has_the_field_the_share_link_fills_and_a_required_consent_box():
    form = yaml.safe_load((REPO / ".github" / "ISSUE_TEMPLATE" / measure.FORM_TEMPLATE).read_text(encoding="utf-8"))
    fields = {f.get("id"): f for f in form["body"]}
    assert fields[measure.FORM_FIELD]["type"] == "textarea"
    assert fields[measure.FORM_FIELD]["validations"]["required"] is True
    assert "results" in form["labels"]
    box = fields["consent"]["attributes"]["options"][0]
    assert box["required"] is True and "CC0" in box["label"] and "public" in box["label"]


def test_a_good_file_becomes_one_row_of_numbers():
    row = cr.check(copy.deepcopy(GOOD))
    assert set(row) == set(cr.COLUMNS) - {"issue"}
    assert (row["picker"], row["picker_right"], row["n"], row["router"], row["router_right"]) == (
        "qwen2.5-coder:7b", 231, 315, "router", 205)
    assert (row["only_picker"], row["only_router"], row["keywords_right"]) == (74, 48, 116)
    assert row["picker_digest_12"] == "a" * 12 and row["gpus"] == "Example GPU"
    assert row["timing_median_s_500"] == 3.2 and row["own_labels_n"] == ""


def test_a_submitted_issue_is_read_only_with_the_consent_box_ticked():
    assert cr.from_issue(body(GOOD)) == GOOD
    with pytest.raises(cr.Rejected, match="consent"):
        cr.from_issue(body(GOOD, ticked=False))
    with pytest.raises(cr.Rejected, match="not JSON"):
        cr.from_issue(body(GOOD, results="my numbers were great"))
    with pytest.raises(cr.Rejected, match="Results file"):
        cr.from_issue("### Sharing\n\n- [X] yes\n\n{}")


def test_a_fence_the_person_added_inside_the_form_is_tolerated():
    assert cr.from_issue(body(GOOD, results="```json\n" + json.dumps(GOOD) + "\n```")) == GOOD


@pytest.mark.parametrize("edit, reason", [
    (lambda d: d["heldout"]["picker"].__setitem__("right", 300), "routes don't add up"),
    (lambda d: d["heldout"]["picker"]["per_route"]["quick"].__setitem__("right", 27), "routes don't add up"),
    (lambda d: d["heldout"]["picker_vs_router"].__setitem__("only_first", 70), "doesn't match the two right counts"),
    (lambda d: d["heldout"]["router"]["confusion"]["code"].__setitem__("quick", 40), "confusion.code"),
    (lambda d: d.__setitem__("prompts", ["a secret"]), "unexpected ['prompts']"),
    (lambda d: d["machine"].__setitem__("host", "my-laptop"), "unexpected ['host']"),
    (lambda d: d["settings"].__setitem__("heldout_sha256_16", "0" * 16), "different held-out set"),
    (lambda d: d.__setitem__("schema", "trioditis-results.v2"), "schema"),
    (lambda d: d.__setitem__("date", "2026-02-30"), "real date"),
    (lambda d: d.__setitem__("date", "2999-01-01"), "future"),
    (lambda d: d["heldout"]["picker"].__setitem__("n", 1.5), "whole number"),
    (lambda d: d["machine"].__setitem__("cores", True), "whole number"),
    (lambda d: d["models"].__setitem__("picker", "model `rm -rf`"), "unexpected text"),
], ids=["total", "per-route", "paired", "confusion", "extra-top-key", "extra-machine-key", "heldout-hash",
        "schema", "bad-date", "future-date", "float-count", "bool-count", "shell-in-name"])
def test_a_file_that_doesnt_add_up_or_holds_more_than_numbers_is_left_out(edit, reason):
    doc = copy.deepcopy(GOOD)
    edit(doc)
    assert reason in rejected(doc)


@pytest.mark.parametrize("text, reason", [
    ('{"a": NaN}', "not a number"),
    ('{"a": "' + "x" * 200 + '"}', "longer than"),
    ('{"a": "line\\nbreak"}', "characters"),
    ('{"a": "<script>"}', "characters"),
    ("[" * 20 + "]" * 20, "nested deeper"),
    ("1" * (cr.MAX_BODY + 1), "longer than"),
])
def test_text_is_bounded_before_any_field_is_read(text, reason):
    with pytest.raises(cr.Rejected, match=reason):
        cr.parse(text)


def test_a_spreadsheet_never_runs_a_cell_as_a_formula():
    doc = copy.deepcopy(GOOD)
    doc["machine"]["cpu"] = "=HYPERLINK(\"http://x\")"
    out = cr.render({7: {"issue": 7, **cr.check(doc)}})
    assert ',"\'=HYPERLINK' in out and ',=HYPERLINK' not in out and ',"=HYPERLINK' not in out


def test_collect_shows_first_writes_only_with_write_and_keeps_rows_already_there(tmp_path, capsys):
    issues = tmp_path / "issues.json"
    sheet = tmp_path / "community-results.csv"
    issues.write_text(json.dumps([{"number": 12, "body": body(GOOD)},
                                  {"number": 13, "body": body(GOOD, ticked=False)},
                                  {"number": "x", "body": ""}]), encoding="utf-8")
    assert cr.main(["collect", str(issues), "--csv", str(sheet)]) == 0
    out = capsys.readouterr().out
    assert "#12: new row: qwen2.5-coder:7b 231/315" in out and "#13: left out: the consent box" in out
    assert "nothing written" in out and not sheet.exists()

    assert cr.main(["collect", str(issues), "--csv", str(sheet), "--write"]) == 0
    first = sheet.read_text(encoding="utf-8")
    assert first.splitlines()[0] == ",".join(cr.COLUMNS) and first.splitlines()[1].startswith("12,2026-11-24,")
    assert cr.main(["collect", str(issues), "--csv", str(sheet), "--write"]) == 0
    assert sheet.read_text(encoding="utf-8") == first
    assert "nothing new" in capsys.readouterr().out


def test_the_shipped_csv_has_the_columns_the_script_writes():
    header = (REPO / "docs" / "community-results.csv").read_text(encoding="utf-8").splitlines()[0]
    assert header == ",".join(cr.COLUMNS)


def test_check_exits_1_when_a_file_is_left_out(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("{}", encoding="utf-8")
    assert cr.main(["check", str(REPO / "tests" / "fixtures" / "results-v1.json")]) == 0
    assert cr.main(["check", str(bad)]) == 1
    assert "left out: file: missing" in capsys.readouterr().out
