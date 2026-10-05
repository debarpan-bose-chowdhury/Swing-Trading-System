"""Phase 5: the one-shot holdout, promotion (overlay, rollback, diff, dossier), the shadow comparison, and the candidate report's holdout panel."""

import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from backtest import api
from hpo import gate, holdout, promote, shadow
from hpo import ledger as ledger_mod
from hpo.errors import Failed, Refusal
from hpo.evalpool import EvalPool
from hpo.study import Study, read_records
from hpo.viz import candidate
from tests.hpo.fakes import FakeRunner, REPO, make_settings
from tests.hpo.test_space import load_space

ACTIVE = ["risk.stops.atrMultiplier", "risk.sizing.riskPerPositionPct", "risk.sizing.minNewOrderInr", "risk.sizing.nameCapPct.MidCap", "risk.sizing.noTradeBand.relative",
          "analyst.strategies.BULL.top_n", "risk.cooldown.stopTradingDays"]


class Case(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        *_, cls.sp = load_space()

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.data = Path(tmp.name) / "data"
        self.cfg = make_settings(str(self.data))

    def candidate(self, passed=True, audit_clean=True, name="p"):
        """A study, a candidate and its saved gate evidence; the verdict is forced so these tests are about what happens after the gate."""
        st = Study.create(self.cfg, self.sp, {"name": name, "stage": "B", "sampler": "sobol", "active": ACTIVE, "trials": 40, "seed": 4, "variants": 0})
        st.run(FakeRunner, workers=1, out=io.StringIO())
        rec = max([r for r in read_records(st.trials_path) if r["feasible"]], key=lambda r: r["objectives"][0])
        led = ledger_mod.Ledger(self.data / "ledger" / "l.json", 200, 0.3)
        led.record(name, "B", 40, 12)
        ev = gate.collect(st, EvalPool(FakeRunner, 1), self.cfg, rec, led)
        verdict, aud = gate.checks(ev, self.cfg), gate.audit(ev, self.cfg)
        verdict["passed"], verdict["failed"] = passed, [] if passed else ["pbo"]
        aud["passed"], aud["flagged"] = audit_clean, [] if audit_clean else ["cliffs"]
        folder = self.data / "candidates" / rec["trialId"]
        gate.save(folder, ev, verdict, aud, self.cfg)
        return st, rec, folder

    def score(self, st, rec, **runner):
        return holdout.score(st, EvalPool(lambda: FakeRunner(**runner), 1), self.cfg, rec["trialId"], rec)


class HoldoutTests(Case):
    def test_the_criterion_is_registered_at_gate_time_and_its_hash_is_in_the_gate_report(self):
        st, rec, folder = self.candidate()
        crit = json.loads((folder / "criterion.json").read_text())
        self.assertEqual(holdout.sha_of(crit), json.loads((folder / "gate.json").read_text())["criterionSha"])
        self.assertLessEqual(crit["maxDepth"], 0.30)
        self.assertEqual(crit["candidate"], rec["trialId"])

    def test_only_a_candidate_that_passed_the_gate_with_a_clean_audit_is_scored(self):
        for kw in ({"passed": False}, {"audit_clean": False}):
            st, rec, folder = self.candidate(name="p" + str(len(kw)) + list(kw)[0][:3], **kw)
            with self.assertRaisesRegex(Refusal, "gate passed"):
                self.score(st, rec)
            self.assertFalse((self.data / "holdout.marker").exists())

    def test_scored_once_then_never_again_and_not_for_a_second_parameter_set(self):
        st, rec, folder = self.candidate()
        out = self.score(st, rec)
        self.assertEqual((out["status"], out["passed"]), ("scored", True))
        self.assertTrue((self.data / "holdout.marker").exists() and (folder / "holdout_returns.parquet").exists())
        with self.assertRaisesRegex(Refusal, "once only"):
            self.score(st, rec)
        other = Study.create(self.cfg, self.sp, {"name": "q", "stage": "B", "sampler": "sobol", "active": ACTIVE, "trials": 40, "seed": 9, "variants": 0})
        other.run(FakeRunner, workers=1, out=io.StringIO())
        rec2 = max([r for r in read_records(other.trials_path) if r["feasible"] and r["pointKey"] != rec["pointKey"]], key=lambda r: r["objectives"][0])
        led = ledger_mod.Ledger(self.data / "ledger" / "l.json", 200, 0.3)
        ev = gate.collect(other, EvalPool(FakeRunner, 1), self.cfg, rec2, led)
        v, a = gate.checks(ev, self.cfg), gate.audit(ev, self.cfg)
        v["passed"], a["passed"] = True, True
        gate.save(self.data / "candidates" / rec2["trialId"], ev, v, a, self.cfg)
        with self.assertRaisesRegex(Refusal, "another parameter set"):
            holdout.score(other, EvalPool(FakeRunner, 1), self.cfg, rec2["trialId"], rec2)

    def test_a_criterion_changed_after_the_gate_stops_the_holdout(self):
        st, rec, folder = self.candidate()
        crit = json.loads((folder / "criterion.json").read_text())
        crit["minCagr"] = -0.5  # a criterion loosened after the fact
        (folder / "criterion.json").write_text(json.dumps(crit))
        with self.assertRaisesRegex(Refusal, "missing or changed"):
            self.score(st, rec)
        self.assertFalse((self.data / "holdout.marker").exists())

    def test_catastrophic_holdout_fails_the_criterion(self):
        st, rec, folder = self.candidate()
        out = self.score(st, rec, holdout_cagr=-0.05)
        self.assertFalse(out["passed"])
        self.assertFalse(out["checks"]["cagrPositive"]["passed"])
        self.assertIn("holdout criterion not met", promote.unmet(folder))

    def test_a_crash_after_the_look_is_recorded_and_the_look_is_not_repeated(self):
        st, rec, folder = self.candidate()
        with self.assertRaisesRegex(Failed, "look was taken"):
            self.score(st, rec, holdout_error=True)
        self.assertEqual(json.loads((folder / "holdout.json").read_text())["status"], "error")
        with self.assertRaisesRegex(Refusal, "once only"):
            self.score(st, rec)

    def test_evidence_is_frozen_once_the_holdout_is_scored(self):
        st, rec, folder = self.candidate()
        self.score(st, rec)
        with self.assertRaisesRegex(Refusal, "frozen"):
            gate.save(folder, {}, {}, {}, self.cfg)

    def test_evaluate_applies_every_clause_of_the_criterion(self):
        crit = {"minCagr": 0.0, "maxDepth": 0.2, "minFillsPerYear": 15, "maxCagrBelowDefault": 0.02}
        good = {"metrics": {"cagr": 0.05}, "fills": 60, "years": 2.0, "depth": 0.1}
        base = {"metrics": {"cagr": 0.06}, "fills": 0, "years": 2.0, "depth": 0.0}
        self.assertTrue(holdout.evaluate(crit, good, base)["passed"])
        for change in ({"depth": 0.3}, {"fills": 10}, {"metrics": {"cagr": 0.0}}, {"metrics": {"cagr": 0.02}}):
            self.assertFalse(holdout.evaluate(crit, {**good, **change}, base)["passed"], change)


class PromoteTests(Case):
    def promoted(self):
        st, rec, folder = self.candidate()
        self.score(st, rec)
        before = hashlib.sha256(b"".join(p.read_bytes() for p in sorted((REPO / "app/config").glob("*.json")))).hexdigest()
        dossier = promote.build(st, folder, self.cfg, self.cfg["paths"]["register"])
        after = hashlib.sha256(b"".join(p.read_bytes() for p in sorted((REPO / "app/config").glob("*.json")))).hexdigest()
        return st, rec, folder, dossier, before == after

    def test_refused_until_gate_audit_and_holdout_are_all_done(self):
        st, rec, folder = self.candidate()
        self.assertEqual(promote.unmet(folder), ["holdout not scored"])
        with self.assertRaisesRegex(Refusal, "holdout not scored"):
            promote.build(st, folder, self.cfg, self.cfg["paths"]["register"])
        st2, rec2, folder2 = self.candidate(passed=False, audit_clean=False, name="z")
        self.assertEqual(len(promote.unmet(folder2)), 3)
        self.assertFalse((folder / "overlay.json").exists())

    def test_files_are_written_app_config_is_untouched_and_the_overlay_holds_only_changes(self):
        st, rec, folder, dossier, untouched = self.promoted()
        self.assertTrue(untouched)
        for name in ("overlay.json", "rollback.json", "diff.md", "dossier.md", "dossier.json"):
            self.assertTrue((folder / name).exists(), name)
        ov = json.loads((folder / "overlay.json").read_text())
        rb = json.loads((folder / "rollback.json").read_text())
        self.assertEqual(set(ov), {"risk.json", "analyst.json"})
        flat = {**ov["risk.json"], **ov["analyst.json"]}
        self.assertTrue(flat)
        self.assertTrue(all(k not in ("buckets", "costs") and not k.startswith("costs.") for k in flat))
        self.assertEqual(set(rb["risk.json"]), set(ov["risk.json"]))
        diff = (folder / "diff.md").read_text()
        self.assertIn("| file | key | old | new | class | note |", diff)
        self.assertIn("tunable", diff)

    def test_applying_the_overlay_and_then_the_rollback_restores_the_live_config_exactly(self):
        st, rec, folder, dossier, _ = self.promoted()
        ov = json.loads((folder / "overlay.json").read_text())
        rb = json.loads((folder / "rollback.json").read_text())
        space = st.space
        risk = promote.apply(space.base_risk, ov["risk.json"])
        analyst = promote.apply(space.base_analyst, ov["analyst.json"])
        cand_risk, cand_analyst, _ = space.decode(rec["values"])
        self.assertEqual(promote.flatten({k: v for k, v in risk.items() if k not in ("buckets", "costs")}), promote.flatten({k: v for k, v in cand_risk.items() if k not in ("buckets", "costs")}))
        self.assertEqual(promote.flatten(analyst), promote.flatten(cand_analyst))
        self.assertEqual(promote.apply(risk, rb["risk.json"]), space.base_risk)  # the rollback rehearsal
        self.assertEqual(promote.apply(analyst, rb["analyst.json"]), space.base_analyst)

    def test_the_dossier_fixes_the_shadow_limits_and_carries_the_honest_edge_statement(self):
        st, rec, folder, d, _ = self.promoted()
        self.assertEqual((d["shadow"]["weeks"], d["shadow"]["trackingGapPp"]), (13, 2.0))
        self.assertTrue(0 < d["shadow"]["drawdownRollbackDepth"] < 1)
        self.assertIn("unlikely to yield a statistically demonstrable improvement", d["honestEdge"])
        self.assertTrue(d["holdout"]["passed"])
        self.assertEqual(d["effectiveN"], ledger_mod.Ledger(self.data / "ledger" / "research_ledger.json", 200, 0.3).effective_total())
        self.assertGreater(d["effectiveN"], 0)
        md = (folder / "dossier.md").read_text()
        for needle in ("Holdout (scored once)", "Shadow period", "not assessed", "unlikely to yield"):
            self.assertIn(needle, md)
        self.assertEqual(promote.unmet(folder), [])

    def test_flatten_walks_dicts_and_ladder_levels_and_keeps_number_pairs_whole(self):
        f = promote.flatten({"a": {"b": 1, "c": [0.1, 0.2]}, "levels": [{"d": 1}, {"d": 2}], "e": "x"})
        self.assertEqual(f, {"a.b": 1, "a.c": [0.1, 0.2], "levels.0.d": 1, "levels.1.d": 2, "e": "x"})


class ShadowTests(unittest.TestCase):
    def series(self, step, n=70, start="2026-01-05"):
        days = [d.strftime("%Y-%m-%d") for d in pd.bdate_range(start, periods=n)]
        return pd.Series(np.cumprod(np.full(n, 1 + step)) * 100, index=days)

    def test_a_shadow_that_tracks_the_replay_is_ok_and_one_that_drifts_must_roll_back(self):
        replay = self.series(0.0005)
        ok = shadow.verdict(shadow.tracking(self.series(0.00049), replay), 2.0, 0.2, 13)
        self.assertEqual(ok["status"], "complete")
        self.assertLess(ok["worstGapPp"], 2.0)
        drift = shadow.verdict(shadow.tracking(self.series(0.0001), replay), 2.0, 0.2, 13)
        self.assertEqual(drift["status"], "rollback")
        self.assertTrue(any("tracking gap" in r for r in drift["reasons"]))

    def test_a_drawdown_past_the_backtest_line_rolls_back_even_when_tracking_is_close(self):
        days = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2026-01-05", periods=70)]
        path = pd.Series(np.r_[np.linspace(100, 70, 35), np.linspace(70, 72, 35)], index=days)
        v = shadow.verdict(shadow.tracking(path, path * 1.0), 2.0, 0.20, 13)
        self.assertEqual(v["status"], "rollback")
        self.assertTrue(any("drawdown" in r for r in v["reasons"]))

    def test_weeks_so_far_and_no_data(self):
        replay = self.series(0.0005, n=20)
        v = shadow.verdict(shadow.tracking(replay, replay), 2.0, 0.2, 13)
        self.assertEqual((v["status"], v["weeks"]), ("ok", 4))
        self.assertEqual(shadow.verdict(shadow.tracking(replay.iloc[:1], replay.iloc[:1]), 2.0, 0.2, 13)["status"], "no data")


class ReportPanelTests(Case):
    def test_the_holdout_panel_is_a_locked_placeholder_before_the_look_and_a_drawn_result_after(self):
        st, rec, folder = self.candidate()
        before = candidate.candidate_report(folder, self.cfg).read_text(encoding="utf-8")
        self.assertIn("locked: not scored", before)
        self.assertNotIn('id="plot-c24"', before)
        self.score(st, rec)
        after = candidate.candidate_report(folder, self.cfg).read_text(encoding="utf-8")
        self.assertIn('id="plot-c24"', after)
        self.assertIn("Holdout scored once", after)
        self.assertIn("2024-01-02", after)


if __name__ == "__main__":
    unittest.main()
