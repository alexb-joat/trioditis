import datetime as dt
import json
import time
from types import SimpleNamespace

import pytest

from trioditis.config import ConfigError, parse_config
from trioditis.pricefacts import check_lines, lifecycle_report, retirement
from trioditis.refit import MIN_DAY_CALLS, WARMUP_DAYS, findings, fit, new_findings, run
from trioditis_metrics import MetricsDB
from trioditis_metrics.db import open_readonly

TODAY = dt.date(2026, 11, 2)

FACTS = {
    "$schema": "provider-facts.v1", "generatedAt": "2026-10-30",
    "providers": {
        "acme": {
            "sources": {"lifecycle": {"url": "https://acme.example/deprecations", "fetched": "2026-10-30"},
                        "pricing": {"url": "https://acme.example/pricing", "fetched": "2026-10-30"}},
            "models": {
                "acme/old": {"apiId": "old-1", "status": "deprecated", "retiresAt": "2026-11-19",
                             "sources": {"status": "lifecycle", "price": "pricing"},
                             "price": {"input": 1.0, "output": 4.0}},
                "acme/legacy": {"apiId": "legacy-2", "status": "legacy", "retiresAt": "not before 2026-11-24",
                                "sources": {"status": "lifecycle", "price": "pricing"},
                                "price": {"input": 2.0, "output": 8.0}},
                "acme/fine": {"apiId": "fine-3", "status": "active", "retiresAt": "not before 2027-09-01",
                              "sources": {"status": "lifecycle", "price": "pricing"},
                              "price": {"input": 3.0, "output": 15.0}},
                "acme/new": {"apiId": "new-4", "status": "active", "sources": {"price": "pricing"},
                             "price": {"input": 0.5, "output": 2.0}},
            },
        },
        "other": {"sources": {}, "models": {
            "other/x": {"apiId": "x-9", "status": "active", "note": "a provider the config doesn't use"}}},
        "chat": {"sources": {"app": {"url": "https://chat.example/models", "fetched": "2026-10-14"}}, "models": {
            "chat/gone": {"apiId": "gone-5", "status": "retired", "retiresAt": "2026-10-14",
                          "sources": {"status": "app"}, "note": "retired from the chat app; still on the API"}}},
    },
}


def test_retirement_tells_a_set_date_from_an_earliest_one():
    assert retirement("2026-11-19") == (dt.date(2026, 11, 19), True)
    assert retirement("not before 2027-06-09") == (dt.date(2027, 6, 9), False)
    assert retirement(None) is None and retirement("soon") is None


def test_lifecycle_report_quotes_the_fact_and_its_source():
    report = lifecycle_report(FACTS, {"a": "old-1", "b": "legacy-2", "c": "fine-3", "d": "gone-5", "e": "missing-0"},
                              TODAY)
    why = {w["model"]: w["why"] for w in report["warnings"]}
    assert why == {"a": "listed as deprecated; retires on 2026-11-19",
                   "b": "may retire from 2026-11-24 (the provider says not before)",
                   "d": "listed as retired; retired on 2026-10-14"}
    gone = next(w for w in report["warnings"] if w["model"] == "d")
    assert gone["note"].endswith("still on the API") and gone["source"] == "https://chat.example/models"
    assert report["unknown"] == [{"model": "e", "api_id": "missing-0"}]
    # only active models of providers already in use, never the configured ones
    assert [c["api_id"] for c in report["challengers"]] == ["new-4"]


def test_check_lines_never_raise(tmp_path):
    assert "cannot use" in check_lines(str(tmp_path / "missing.json"), {"a": "old-1"})[0]
    path = tmp_path / "facts.json"
    path.write_text(json.dumps(FACTS))
    lines = check_lines(str(path), {"a": "old-1"}, TODAY)
    assert lines == ["model facts: a (old-1): listed as deprecated; retires on 2026-11-19 "
                     "[https://acme.example/deprecations, 2026-10-30]"]


def config(facts=None):
    raw = {"providers": {"local": {"base_url": "http://localhost:11434/v1", "local": True},
                         "acme": {"base_url": "https://api.acme.example/v1", "api_key_env": "ACME_KEY"}},
           "models": {"small": {"provider": "local", "id": "llama3.2:3b"},
                      "cheap": {"provider": "acme", "id": "fine-3"},
                      "dated": {"provider": "acme", "id": "old-1"}},
           "routes": {"quick": {"models": ["small", "cheap", "dated"]}}, "default_route": "quick",
           "logging": {"path": None}}
    if facts:
        raw["facts"] = facts
    return parse_config(raw)


def test_config_takes_a_facts_path():
    assert config("~/facts.json").facts.endswith("/facts.json") and "~" not in config("~/facts.json").facts
    assert config().facts is None
    with pytest.raises(ConfigError, match="facts"):
        config(["not", "a", "path"])


def calls_db(tmp_path, days: int, spike_from: int | None = None):
    db = MetricsDB(tmp_path / "metrics.db")
    start = time.mktime((2026, 10, 1, 12, 0, 0, 0, 0, -1))
    for day in range(days):
        for n in range(20):
            failed = n < (10 if spike_from is not None and day >= spike_from else 1)  # 5% errors, then 50%
            db.record({"route": "quick", "model": "cheap", "local": False, "status": 500 if failed else 200,
                       "latency_ms": 1000 + n * 10, "usage": None if failed else {"prompt_tokens": 10}},
                      ts=start + day * 86400 + n)
    db.close()
    return start + days * 86400


def test_fit_reports_rates_latency_and_a_clean_chart(tmp_path):
    now = calls_db(tmp_path, days=WARMUP_DAYS + 5)
    conn = open_readonly(tmp_path / "metrics.db")
    doc = fit(conn, config(), None, {}, now, days=28)
    m = doc["models"]["cheap"]
    assert m["calls"] == 20 * (WARMUP_DAYS + 5)
    err = m["rates"]["error"]
    assert err["value"] == pytest.approx(0.05) and err["lo"] < 0.05 < err["hi"]
    assert m["latency_ms"]["n"] == 19 * (WARMUP_DAYS + 5) and m["latency_ms"]["p50"] is not None
    assert m["daily_errors"]["status"] == "in control" and m["daily_errors"]["alarms"] == []
    assert doc["lifecycle"] is None and doc["proposals"] == []
    assert set(doc["methods"]) >= {"rates", "daily_errors"}


def test_fit_flags_a_lasting_rise_in_errors_once(tmp_path):
    now = calls_db(tmp_path, days=WARMUP_DAYS + 6, spike_from=WARMUP_DAYS + 2)
    doc = fit(open_readonly(tmp_path / "metrics.db"), config(), None, {}, now)
    chart = doc["models"]["cheap"]["daily_errors"]
    assert chart["status"] == "out of limits" and chart["alarms"] == ["2026-10-13"]
    assert [f["kind"] for f in findings(doc)] == ["drift"]


def test_fit_says_when_there_is_too_little_data_to_chart(tmp_path):
    now = calls_db(tmp_path, days=3)
    chart = fit(open_readonly(tmp_path / "metrics.db"), config(), None, {}, now)["models"]["cheap"]["daily_errors"]
    assert chart["status"] == "not enough data" and str(MIN_DAY_CALLS) in chart["needs"]


def test_fit_lists_lifecycle_and_price_proposals_without_applying_them(tmp_path):
    now = calls_db(tmp_path, days=1)
    prices = {"cheap": {"input": 3.0, "output": 15.0}, "dated": {"input": 0.9, "output": 4.0}}
    doc = fit(open_readonly(tmp_path / "metrics.db"), config("/facts.json"), FACTS, prices, now, today=TODAY)
    assert [w["model"] for w in doc["lifecycle"]["warnings"]] == ["dated"]
    assert doc["proposals"] == [{"kind": "price", "model": "dated", "api_id": "old-1",
                                 "from": {"input": 0.9, "output": 4.0}, "to": {"input": 1.0, "output": 4.0},
                                 "source": "https://acme.example/pricing", "fetched": "2026-10-30",
                                 "apply": "trioditis prices import /facts.json"}]
    assert {f["kind"] for f in findings(doc)} == {"lifecycle", "challenger", "price"}
    assert new_findings(doc, doc) == []  # the next refit with nothing changed adds nothing


def test_run_writes_the_fitted_file_and_only_new_findings(tmp_path, capsys):
    calls_db(tmp_path, days=1)
    facts = tmp_path / "facts.json"
    facts.write_text(json.dumps(FACTS))
    args = SimpleNamespace(db=str(tmp_path / "metrics.db"), out=str(tmp_path / "fitted.json"),
                           notebook=str(tmp_path / "notebook.jsonl"), prices=str(tmp_path / "prices.json"),
                           days=400, json=False, dry_run=True)
    assert run(config(str(facts)), args) == 0
    assert not (tmp_path / "fitted.json").exists() and "dry run" in capsys.readouterr().out
    args.dry_run = False
    run(config(str(facts)), args)
    doc = json.loads((tmp_path / "fitted.json").read_text())
    assert doc["$schema"] == "trioditis-fitted.v1" and doc["models"]["cheap"]["calls"] == 20
    first = (tmp_path / "notebook.jsonl").read_text().splitlines()
    assert first and all(json.loads(line)["system"] == "trioditis" for line in first)
    run(config(str(facts)), args)
    assert (tmp_path / "notebook.jsonl").read_text().splitlines() == first  # nothing new, nothing added
    assert "nothing new" in capsys.readouterr().out


def test_run_without_a_database_says_so(tmp_path, capsys):
    args = SimpleNamespace(db=str(tmp_path / "none.db"), out=str(tmp_path / "f.json"),
                           notebook=str(tmp_path / "n.jsonl"), prices=None, days=28, json=False, dry_run=False)
    assert run(config(), args) == 0
    assert "no metrics database" in capsys.readouterr().out and not (tmp_path / "f.json").exists()


def test_check_names_retiring_models_from_the_facts(tmp_path, capsys):
    import yaml

    from trioditis.__main__ import main
    facts = tmp_path / "facts.json"
    facts.write_text(json.dumps({**FACTS, "generatedAt": dt.date.today().isoformat()}))
    raw = {"providers": {"acme": {"base_url": "https://api.acme.example/v1", "api_key_env": "ACME_KEY"}},
           "models": {"dated": {"provider": "acme", "id": "gone-5"}},
           "routes": {"quick": {"models": ["dated"]}}, "default_route": "quick",
           "logging": {"path": None}, "facts": str(facts)}
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(raw))
    main(["--config", str(tmp_path / "config.yaml"), "check"])
    out = capsys.readouterr().out
    assert "model facts: dated (gone-5): listed as retired" in out and "still on the API" in out
