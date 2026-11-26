"""scripts/collect_results.py: shared results are untrusted data; only checked, consented files become rows."""

import copy
import csv
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


def no_ollama(doc: dict) -> dict:
    """What `trioditis results` writes for a server that is not Ollama: no /api/version, so no version and no digests."""
    doc["models"].update(server=None, server_version=None, picker_digest=None, embeddings_digest=None)
    return doc


def test_a_file_from_a_server_that_is_not_ollama_becomes_one_row_labelled_unknown_server(tmp_path, capsys):
    doc = no_ollama(copy.deepcopy(GOOD))
    row = cr.check(doc)
    assert row["server"] == "unknown server" and row["server_version"] == "" and row["picker_digest_12"] == ""
    assert cr.check(copy.deepcopy(GOOD))["server"] == "ollama"                       # an Ollama row is as before
    issues = tmp_path / "issues.json"
    sheet = tmp_path / "community-results.csv"
    issues.write_text(json.dumps([{"number": 20, "body": body(doc)}, {"number": 21, "body": body(GOOD)}]), encoding="utf-8")
    assert cr.main(["collect", str(issues), "--csv", str(sheet), "--write"]) == 0
    out = capsys.readouterr().out
    assert "#20: new row" in out and "[unknown server: keep apart from the Ollama rows]" in out
    assert "#21: new row" in out and out.count("keep apart") == 1
    with sheet.open(newline="", encoding="utf-8") as f:
        rows = {int(r["issue"]): r for r in csv.DictReader(f)}
    assert (rows[20]["server"], rows[21]["server"]) == ("unknown server", "ollama")
    assert rows[20]["picker_right"] == rows[21]["picker_right"] == "231"              # same numbers, kept as separate kinds of row


@pytest.mark.parametrize("server", [5, True, 1.5, ["ollama"], {"name": "ollama"}, "", "unknown server", "has space",
                                    "x" * 101, "<script>"])
def test_a_server_that_is_neither_text_nor_null_is_still_left_out(server):
    doc = copy.deepcopy(GOOD)
    doc["models"]["server"] = server
    assert "models.server" in rejected(doc) or "characters" in rejected(doc)


def test_the_unknown_server_label_cannot_be_written_as_a_server_name():
    """No valid server name contains a space, so no file can pass itself off as the label (or as an Ollama row
    with a lookalike name: names are checked against the same pattern as before)."""
    doc = copy.deepcopy(GOOD)
    doc["models"]["server"] = "unknown server"
    assert "models.server: unexpected text" in rejected(doc)


@pytest.mark.parametrize("key, value", [("server_version", "0.13.0"), ("picker_digest", "a" * 64),
                                        ("embeddings_digest", "b" * 64)])
def test_a_null_server_with_a_version_or_a_digest_is_left_out(key, value):
    doc = no_ollama(copy.deepcopy(GOOD))
    doc["models"][key] = value
    assert "models.server is null" in rejected(doc)


def test_every_other_check_still_applies_to_a_file_with_a_null_server():
    for edit, reason in [(lambda d: d["heldout"]["picker"].__setitem__("right", 300), "routes don't add up"),
                         (lambda d: d["settings"].__setitem__("heldout_sha256_16", "0" * 16), "different held-out set"),
                         (lambda d: d.__setitem__("prompts", ["a secret"]), "unexpected ['prompts']"),
                         (lambda d: d["models"].__setitem__("picker", "model `rm -rf`"), "unexpected text"),
                         (lambda d: d.__setitem__("date", "2999-01-01"), "future")]:
        doc = no_ollama(copy.deepcopy(GOOD))
        edit(doc)
        assert reason in rejected(doc)
    assert "consent" in str(pytest.raises(cr.Rejected, cr.from_issue, body(no_ollama(copy.deepcopy(GOOD)), ticked=False)).value)


def test_nothing_in_the_repository_sums_the_community_sheet_yet():
    """If a script or a doc starts to total or average docs/community-results.csv, it must keep the rows labelled
    `unknown server` apart from the Ollama rows, and this test should be replaced by one that checks that."""
    readers = [p.relative_to(REPO).as_posix() for p in [*(REPO / "scripts").glob("*.py"), *(REPO / "trioditis").glob("*.py"),
                                                         *(REPO / "trioditis_metrics").glob("*.py")]
               if "community-results" in p.read_text(encoding="utf-8")]
    assert readers == ["scripts/collect_results.py"]


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


# ---- found by the review before 0.8.0: files whose counts agree with each other but cannot be true, text a
# ---- terminal or spreadsheet would act on, and one submission that stopped or emptied the whole batch

def tiny(doc: dict) -> dict:
    """Counts that agree with each other but not with the shipped set: 4 prompts, one per route, all right."""
    def scored() -> dict:
        one, four = cr.wilson(1, 1), cr.wilson(4, 4)
        return {"right": 4, "n": 4, "low95": round(four[0], 4), "high95": round(four[1], 4),
                "per_route": {r: {"right": 1, "n": 1, "low95": round(one[0], 4), "high95": round(one[1], 4)}
                              for r in cr.ROUTES},
                "confusion": {r: {r: 1} for r in cr.ROUTES}}
    h = doc["heldout"]
    h.update(n=4, router=None, router_keywords=scored(),
             picker={**scored(), "no_route": 0, "failed_after_retry": 0, "seconds_per_call": h["picker"]["seconds_per_call"]},
             picker_vs_router={"only_first": 0, "only_second": 0, "sign_test_p": 1.0, "router": "router_keywords"})
    return doc


def test_a_file_must_count_the_315_shipped_prompts_route_by_route():
    assert sum(cr.heldout_counts().values()) == 315 and set(cr.heldout_counts()) == set(cr.ROUTES)
    assert "the shipped held-out set has 315 prompts" in rejected(tiny(copy.deepcopy(GOOD)))
    doc = copy.deepcopy(GOOD)
    for name in ("picker", "router_keywords", "router"):
        per = doc["heldout"][name]["per_route"]
        per["code"]["n"] += 1
        per["quick"]["n"] -= 1
    assert f"per_route.code.n is {cr.heldout_counts()['code'] + 1}, expected {cr.heldout_counts()['code']}" in rejected(doc)


@pytest.mark.parametrize("edit, reason", [
    (lambda d: d["heldout"]["picker"].update(low95=0.99, high95=0.01), "is not the 95% interval of 231 of 315"),
    (lambda d: d["heldout"]["picker"]["per_route"]["code"].update(high95=1.0), "is not the 95% interval"),
    (lambda d: d["heldout"]["picker_vs_router"].update(sign_test_p=0.0), "is not the sign test of 74 against 48"),
    (lambda d: d["heldout"]["picker_vs_router"].update(only_first=315, only_second=289), "leave room for"),
    (lambda d: d["heldout"]["picker"].update(no_route=315), "don't add up to the answers without a route"),
    (lambda d: d.update(date="0001-01-01"), "did not exist yet"),
    (lambda d: d.update(trioditis="０.８.０"), "unexpected text"),
    (lambda d: d["machine"].update(python="३.१२.१"), "unexpected text"),
], ids=["interval", "route-interval", "sign-test", "paired-too-many", "no-route", "old-date", "wide-digits", "other-digits"])
def test_numbers_that_cannot_be_true_are_left_out(edit, reason):
    doc = copy.deepcopy(GOOD)
    edit(doc)
    assert reason in rejected(doc)


def test_a_tiny_p_value_is_accepted_rounded_to_zero_or_written_in_full():
    exact = cr.sign_test(115, 0)
    assert 0 < exact < 1e-30
    for p in (0.0, exact, float(f"{exact:.6g}")):         # rounded to 6 places, the value itself, 6 significant digits
        doc = copy.deepcopy(GOOD)
        h = doc["heldout"]                                # set against the keyword row: 231 - 116 = 115 - 0
        h.update(router=None, picker_vs_router={"only_first": 115, "only_second": 0, "sign_test_p": p,
                                                "router": "router_keywords"})
        assert cr.check(doc)["sign_test_p"] == p
    doc["heldout"]["picker_vs_router"]["sign_test_p"] = 0.001
    assert "is not the sign test of 115 against 0" in rejected(doc)


@pytest.mark.parametrize("name", ["Example \u202e GPU", "Example \u009b GPU", "Example \u2028 GPU", "Example \u0085 GPU",
                                  "Example \u200b GPU", "Example \ud800 GPU", "\ufeffExample GPU"])
def test_hidden_marks_and_lone_surrogates_are_left_out(name):
    text = json.dumps(GOOD).replace("Example GPU", name)
    assert name in text
    with pytest.raises(cr.Rejected, match="characters a results file never holds"):
        cr.parse(text)


@pytest.mark.parametrize("text, reason", [
    ('{"n": 315, "n": 4}', "twice"),
    ('{"a": ' + "9" * 5000 + "}", "too long to read"),
    ('{"a": -0.0}', "negative zero"),
], ids=["repeated-key", "huge-number", "negative-zero"])
def test_json_that_reads_two_ways_or_not_at_all_is_left_out(text, reason):
    with pytest.raises(cr.Rejected, match=reason):
        cr.parse(text)


def test_one_unreadable_submission_does_not_stop_the_ones_after_it():
    new, notes = cr.collect([{"number": 40, "body": body(GOOD, results='{"a": ' + "9" * 5000 + "}")},
                             {"number": 41, "body": body(GOOD)}], {})
    assert list(new) == [41] and len(notes) == 1 and notes[0].startswith("#40: left out")


def test_a_cell_is_guarded_also_behind_leading_spaces():
    for cell in (" =1+1", "\u00a0=1+1", "  @SUM(1)", "\u3000-1"):
        assert cr._cell(cell) == "'" + cell
    assert cr._cell("Example CPU - 8 cores") == "Example CPU - 8 cores" and cr._cell(0.5) == "0.5"


def test_a_typed_sharing_heading_never_stands_in_for_the_box():
    typed = json.dumps(GOOD) + "\n```\n\n### Sharing\n\n- [x] yes\n\n```"
    with pytest.raises(cr.Rejected, match="more than one 'Sharing' heading"):
        cr.from_issue(body(GOOD, ticked=False, results=typed))


def test_a_surrogate_never_empties_the_sheet_and_the_next_submission_still_lands(tmp_path, capsys):
    issues, sheet = tmp_path / "issues.json", tmp_path / "community-results.csv"
    issues.write_text(json.dumps([{"number": 12, "body": body(GOOD)}]), encoding="utf-8")
    assert cr.main(["collect", str(issues), "--csv", str(sheet), "--write"]) == 0
    before = sheet.read_text(encoding="utf-8")
    bad = json.dumps(GOOD, indent=2).replace("Example GPU", "Example \\ud800 GPU")
    issues.write_text(json.dumps([{"number": 31, "body": body(GOOD, results=bad)}, {"number": 32, "body": body(GOOD)}]),
                      encoding="utf-8")
    assert cr.main(["collect", str(issues), "--csv", str(sheet), "--write"]) == 0
    after = sheet.read_text(encoding="utf-8")
    assert "#31: left out" in capsys.readouterr().out
    assert after.startswith(before) and [line.split(",")[0] for line in after.splitlines()] == ["issue", "12", "32"]


def test_a_write_that_fails_leaves_the_old_sheet_whole(tmp_path, monkeypatch):
    sheet = tmp_path / "community-results.csv"
    sheet.write_text("issue\n12\n", encoding="utf-8")
    with pytest.raises(UnicodeEncodeError):
        cr.write_csv(sheet, "issue\n\ud800\n")
    monkeypatch.setattr(cr.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        cr.write_csv(sheet, "issue\n13\n")
    assert sheet.read_text(encoding="utf-8") == "issue\n12\n"
