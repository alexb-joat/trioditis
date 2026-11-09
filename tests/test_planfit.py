"""Plan weights: the fit recovers known weights from whole-percent readings and stays out of the way of
untracked use, resets and mixed traffic."""

import math
import random

import pytest

from trioditis.planfit import MIN_POINTS, fit_meter, fit_plan, steps, stretches, summary_lines
from trioditis.refit import findings, fit, new_findings
from trioditis_metrics import MetricsDB
from trioditis_metrics.db import open_readonly
from trioditis_metrics.plan import merge_events, merge_meter

STEP = 300.0
T0 = 1_794_000_000.0
A, B = ("claude-opus-5-5", "xhigh"), ("claude-opus-5-5", "max")


class Plan:
    """A simulated account: messages raise a hidden meter by their weight times their uncached tokens,
    and each reading shows that meter rounded down, as the real one does."""

    def __init__(self, weights: dict, seed: int = 7):
        self.weights, self.rng = weights, random.Random(seed)
        self.t, self.level, self.resets_at = T0, 0.3, T0 + 5 * 3600
        self.messages: list[dict] = []
        self.readings: list[tuple] = [(T0, 0.0, self.resets_at)]

    def step(self, group=None, n: int = 2, untracked: float = 0.0, others=()):
        for g in ([group] * n if group else []) + list(others):
            unc = self.rng.randint(20_000, 80_000)
            out = int(unc * self.rng.uniform(0.2, 0.6))
            self.messages.append({"at": self.t + self.rng.uniform(10, STEP - 10), "model": g[0], "effort": g[1],
                                  "tokens_in": 100, "tokens_out": out, "cache_write": unc - out - 100,
                                  "cache_read": self.rng.randint(0, 3_000_000)})
            self.level += self.weights[g] * unc / 1e6
        self.level += untracked
        self.t += STEP
        if self.level >= 95:  # the window resets: a new reset time, the meter from zero
            self.level, self.resets_at = self.level % 1, self.resets_at + 5 * 3600
        self.readings.append((self.t, float(math.floor(self.level)), self.resets_at))

    def fit(self):
        return fit_meter(self.readings, sorted(self.messages, key=lambda m: m["at"]), "2026-11-09")


def alternating(weights, stretches_each=40, seed=7):
    plan = Plan(weights, seed)
    rng = random.Random(seed + 1)
    for i in range(2 * stretches_each):
        for _ in range(rng.randint(1, 3)):
            plan.step(A if i % 2 == 0 else B)
        plan.step()
        plan.step()  # two quiet readings between stretches
    return plan


def test_fit_recovers_known_weights_and_their_ratio():
    m = alternating({A: 10.0, B: 30.0}).fit()
    assert m["basis"] == "uncached"  # the measure the simulated meter counts fits best
    assert m["basis_fit"]["uncached"] < m["basis_fit"]["output"] < m["basis_fit"]["all"]
    a, b = m["groups"]["claude-opus-5-5 xhigh"], m["groups"]["claude-opus-5-5 max"]
    assert a["status"] == b["status"] == "measured" and a["stretches"] >= 30
    for e, true in ((a, 10.0), (b, 30.0)):
        w = e["per_mtok"]
        assert w["lo"] < true < w["hi"] and w["value"] == pytest.approx(true, rel=0.15)
        assert e["evidence"] == {"kind": "measured", "status": "proposed", "date": "2026-11-09",
                                 "method": "trioditis refit: plan weights"}
    assert m["reference"] == "claude-opus-5-5 xhigh" or m["reference"] == "claude-opus-5-5 max"
    other = b if m["reference"] == "claude-opus-5-5 xhigh" else a
    r = other["relative"]
    true_ratio = 3.0 if other is b else 1 / 3
    assert r["lo"] < true_ratio < r["hi"] and r["value"] == pytest.approx(true_ratio, rel=0.2)


def test_untracked_use_next_to_a_stretch_keeps_it_out():
    plan = alternating({A: 10.0, B: 30.0}, stretches_each=5)
    clean = plan.fit()
    plan.step(A)
    plan.step(A)
    plan.step(untracked=0.0)  # the lag reading
    plan.step(untracked=4.0)  # someone else on the account
    plan.step()
    m = plan.fit()
    # the new stretch is out, and so is the one before it: the rise came within 30 minutes of both
    assert m["stretches"]["found"] == clean["stretches"]["found"] + 1
    assert m["stretches"]["quiet"] == clean["stretches"]["quiet"] - 1
    assert m["untracked"]["points"] >= 3 and m["untracked"]["steps"] >= 1


def test_heavy_untracked_use_would_bias_the_weight_but_is_kept_out():
    rng = random.Random(3)
    plan = Plan({A: 10.0})
    for i in range(60):
        busy = 20 <= i < 40  # a stretch of hours where someone else uses the account too
        for _ in range(2):
            plan.step(A, untracked=2.0 if busy else 0.0)
        plan.step(untracked=rng.choice([1.0, 2.0]) if busy else 0.0)
        plan.step(untracked=rng.choice([1.0, 2.0]) if busy else 0.0)
    w = plan.fit()["groups"]["claude-opus-5-5 xhigh"]["per_mtok"]
    assert w["lo"] < 10.0 < w["hi"]


def test_a_reset_splits_and_a_long_gap_is_skipped():
    plan = Plan({A: 10.0})
    plan.level, plan.readings = 94.5, [(T0, 94.0, plan.resets_at)]
    plan.step(A, n=3)  # crosses 95: the window resets inside this step
    plan.step(A)
    plan.t += 3600  # the reader was off for an hour
    plan.step(A)
    st = steps(plan.readings, plan.messages)
    assert [s["kind"] for s in st] == ["reset", "active", "gap"]
    assert st[0]["rise"] is None and len(stretches(st)) == 1


def test_mixed_steps_make_no_stretch_and_are_listed():
    plan = Plan({A: 10.0, B: 10.0})
    for _ in range(10):
        plan.step(A, n=1, others=[B])
        plan.step()
    m = plan.fit()
    assert m["stretches"]["found"] == 0 and m["groups"] == {} and m["basis"] is None
    assert m["unmeasured"] == {"claude-opus-5-5 xhigh": 10, "claude-opus-5-5 max": 10}


def test_lag_reading_counts_toward_the_stretch_before_it():
    st = steps([(0, 0.0, None), (300, 0.0, None), (600, 1.0, None), (900, 1.0, None)],
               [{"at": 100, "model": "m", "effort": "high", "tokens_in": 1, "tokens_out": 1, "cache_read": 0,
                 "cache_write": 0}])
    [s] = stretches(st)
    assert s["rise"] == 1.0 and s["tail"] and s["last"] == 1


def test_too_little_data_says_what_it_needs():
    plan = Plan({A: 1.0})
    for _ in range(4):
        plan.step(A)
        plan.step()
        plan.step()
    e = plan.fit()["groups"]["claude-opus-5-5 xhigh"]
    assert e["status"] == "not enough data" and str(MIN_POINTS) in e["needs"] and "per_mtok" not in e


def plan_db(tmp_path, signals=False):
    db = MetricsDB(tmp_path / "metrics.db")
    plan = alternating({A: 10.0, B: 30.0}, stretches_each=15)
    events = []
    for i, m in enumerate(sorted(plan.messages, key=lambda m: m["at"])):
        ev = {"id": f"m{i}", "at": m["at"], "source": "test", "model": m["model"], "effort": m["effort"],
              "tokens_in": m["tokens_in"], "tokens_out": m["tokens_out"], "cache_read": m["cache_read"],
              "cache_write": m["cache_write"], "tokens_measured": 1}
        if signals and i < 10:
            ev.update(suggested="x", followed=int(i < 6), regenerated=0, vote=1 if i < 3 else 0)
        events.append(ev)
    events.append({"id": "fable", "at": T0 + 60, "source": "test", "model": "claude-fable-5-1", "effort": "high",
                   "tokens_in": 10, "tokens_out": 10, "tokens_measured": 1})
    with db.conn:
        merge_events(db.conn, events)
        merge_meter(db.conn, [{"meter": "session", "at": at, "percent": p, "resets_at": ra}
                              for at, p, ra in plan.readings])
        merge_meter(db.conn, [{"meter": "weekly_scoped", "model": "Fable", "at": at, "percent": 0.0}
                              for at, _, _ in plan.readings])
    db.close()
    return plan.t + 1


def test_fit_plan_reads_the_ledger_and_scopes_a_model_meter(tmp_path):
    now = plan_db(tmp_path)
    doc = fit_plan(open_readonly(tmp_path / "metrics.db"), T0 - 1, now)
    session, fable = doc["meters"]["session"], doc["meters"]["weekly_scoped:Fable"]
    assert session["groups"]["claude-opus-5-5 max"]["status"] == "measured"
    assert fable["scope"] == "Fable" and list(fable["groups"]) == ["claude-fable-5-1 high"]  # only Fable counts
    assert fable["groups"]["claude-fable-5-1 high"]["status"] == "not enough data"
    assert doc["signals"] == {} and "proposed" in doc["review"]
    text = "\n".join(summary_lines(doc))
    assert "points per 1M uncached tokens" in text and "[reference]" in text and "result signals: none" in text


def test_result_signal_rates_have_intervals(tmp_path):
    now = plan_db(tmp_path, signals=True)
    sig = fit_plan(open_readonly(tmp_path / "metrics.db"), T0 - 1, now)["signals"]
    total = {k: sum(g[k]["n"] for g in sig.values() if g[k]) for k in ("followed", "regenerated", "vote_up")}
    assert total == {"followed": 10, "regenerated": 10, "vote_up": 3}  # a vote of 0 is no vote
    for g in sig.values():
        f = g["followed"]
        assert f["lo"] <= f["value"] <= f["hi"] and g["asked_again"] is None


def test_refit_carries_plan_weights_as_proposals_once(tmp_path):
    from tests.test_refit import config
    now = plan_db(tmp_path)
    doc = fit(open_readonly(tmp_path / "metrics.db"), config(), None, {}, now)
    assert "plan_weights" in doc["methods"] and doc["plan"]["meters"]["session"]["basis"] == "uncached"
    props = [f for f in findings(doc) if f["kind"] == "plan-weight"]
    assert {f["subject"] for f in props} == {"claude-opus-5-5 xhigh", "claude-opus-5-5 max"}
    assert all(f["decision"] == "proposal" and "review" in f["finding"] for f in props)
    assert new_findings(doc, doc) == []


def test_an_empty_ledger_has_no_meters(tmp_path):
    MetricsDB(tmp_path / "metrics.db").close()
    assert fit_plan(open_readonly(tmp_path / "metrics.db"), 0, T0)["meters"] == {}
    assert summary_lines(None)[0].startswith("plan weights: no plan ledger yet")
