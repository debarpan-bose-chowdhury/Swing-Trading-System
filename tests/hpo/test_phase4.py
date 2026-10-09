"""Phase 4: statistics, perturbations, plateau-vs-spike selection, the corrected gate (known-overfit fails), evidence for a candidate end to end on a fake runner."""

import io
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from backtest import api
from hpo import gate, ledger as ledger_mod, objective, robust, stats
from hpo.evalpool import EvalPool
from hpo.study import Study, find_trial, read_records
from tests.hpo.fakes import FakeRunner, SpikeRunner, make_settings
from tests.hpo.test_space import load_space

ACTIVE = ["risk.stops.atrMultiplier", "risk.sizing.riskPerPositionPct", "risk.sizing.minNewOrderInr", "risk.sizing.nameCapPct.MidCap", "risk.sizing.noTradeBand.relative"]


def noise(n_series=500, days=800, seed=0):
    rng = np.random.default_rng(seed)
    return pd.DataFrame(rng.normal(0.0002, 0.01, (days, n_series)), columns=[f"t{i}" for i in range(n_series)])


class StatsTests(unittest.TestCase):
    def test_pbo_logits_agree_with_the_backtest_implementation_and_noise_gives_about_one_half(self):
        m = noise(200)
        mine = stats.pbo_logits(m)
        self.assertAlmostEqual(mine["pbo"], api.pbo_cscv(m)["pbo"])
        self.assertEqual(len(mine["logits"]), mine["splits"])
        self.assertTrue(0.3 < mine["pbo"] < 0.7, mine["pbo"])

    def test_the_best_of_500_noise_series_is_not_significant(self):
        m = noise()
        sh = stats.trial_sharpes(m)
        best = m.columns[int(np.argmax(sh))]
        dsr = stats.dsr_curve(m[best].to_numpy(), sh, [500, 1000])
        self.assertLess(dsr[0]["dsr"], 0.95)
        self.assertLess(dsr[1]["dsr"], dsr[0]["dsr"])  # more trials, a heavier penalty
        ps = []  # under the null the p-value is spread over (0, 1): a few datasets, not one lucky or unlucky draw
        for s in range(8):
            n = noise(41, seed=50 + s)
            ps.append(stats.spa_pvalue(n.iloc[:, :40].sub(n.iloc[:, 40], axis=0), draws=200, seed=s)["p"])
        self.assertGreater(float(np.mean(ps)), 0.25)
        self.assertLessEqual(sum(p < 0.05 for p in ps), 2)

    def test_a_clearly_better_trial_has_a_small_spa_p_and_a_high_dsr(self):
        m = noise(60, seed=3)
        m["good"] = np.random.default_rng(9).normal(0.0015, 0.01, len(m))
        excess = m.sub(m["t0"], axis=0).drop(columns=["t0"])
        self.assertLess(stats.spa_pvalue(excess, draws=400)["p"], 0.05)
        self.assertGreater(stats.dsr_curve(m["good"].to_numpy(), stats.trial_sharpes(m), [60])[0]["dsr"], 0.95)

    def test_medoids_cap_the_columns_and_keep_the_candidate(self):
        m = noise(120, days=300)
        small = stats.medoids(m, 20, keep="t119")
        self.assertLessEqual(small.shape[1], 21)
        self.assertIn("t119", small.columns)
        self.assertIs(stats.medoids(m, 500), m)


class PerturbationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        *_, cls.sp = load_space()

    def test_neighbours_are_distinct_valid_points_one_nudge_away_and_reproducible(self):
        base = self.sp.complete(self.sp.defaults)
        a = robust.perturbations(self.sp, base, ACTIVE, ACTIVE, 7, 16, 0.10)
        b = robust.perturbations(self.sp, base, ACTIVE, ACTIVE, 7, 16, 0.10)
        self.assertEqual(a, b)
        self.assertTrue(10 <= len(a) <= 16)
        keys = {self.sp.key(n["values"]) for n in a}
        self.assertEqual(len(keys), len(a))
        self.assertNotIn(self.sp.key(base), keys)
        for n in a:
            moved = [k for k in base if n["values"][k] != base[k]]
            self.assertEqual(sorted(moved), n["changed"])
            if n["kind"] == "single":
                self.assertEqual(len(moved), 1)
            self.sp.decode(n["values"])

    def test_an_integer_moves_one_grid_step_and_a_float_ten_percent(self):
        base = self.sp.complete(self.sp.defaults)
        d = self.sp.dims["analyst.regime.smaFast"]
        self.assertEqual(robust._nudge(self.sp, d.name, base[d.name], True, 0.1), base[d.name] + d.step)
        self.assertAlmostEqual(robust._nudge(self.sp, "risk.stops.atrMultiplier", 3.5, True, 0.1), 4.0)  # 10% is 3.85, snapped to the 0.5 grid
        self.assertNotEqual(robust._nudge(self.sp, "risk.stops.atrMultiplier", 4.5, True, 0.1), 4.5 + 1)  # clipped into the bound, then a step away is the bound itself

    def test_layers_take_successive_fronts(self):
        rows = [{"f1": 0.2, "f2": 0.2, "calmar": 1}, {"f1": 0.1, "f2": 0.1, "calmar": 2}, {"f1": 0.05, "f2": 0.2, "calmar": 0.5}, {"f1": 0.04, "f2": 0.3, "calmar": 0.1}]
        self.assertEqual(len(robust.layers(rows, 2)), 2)
        self.assertEqual({id(r) for r in robust.layers(rows, 3)[2:]}, {id(rows[2])})
        self.assertEqual(len(robust.layers(rows, 10)), 4)

    def test_tolerance_is_per_metric(self):
        tol = {"cagrRel": 0.2, "cagrAbs": 0.02, "ddAbs": 0.03, "ulcerRel": 0.25}
        nom = {"cagr": 0.15, "maxDrawdown": -0.10, "ulcerIndex": 4.0}
        ok = lambda **kw: robust.within(nom, {**nom, **kw}, tol)  # noqa: E731
        self.assertTrue(ok(cagr=0.125, maxDrawdown=-0.12, ulcerIndex=4.9))
        self.assertTrue(ok(cagr=0.30))  # better always passes
        self.assertFalse(ok(cagr=0.11))
        self.assertFalse(ok(maxDrawdown=-0.14))
        self.assertFalse(ok(ulcerIndex=5.2))
        self.assertFalse(robust.within(nom, None, tol))
        self.assertTrue(robust.within({"cagr": 0.01, "maxDrawdown": -0.1, "ulcerIndex": 4.0}, {"cagr": 0.0, "maxDrawdown": -0.1, "ulcerIndex": 4.0}, tol))  # the absolute 2 pp rule on a small CAGR


class Case(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        *_, cls.sp = load_space()

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.cfg = make_settings(str(Path(tmp.name) / "data"))

    def study(self, runner=SpikeRunner, trials=60, name="p"):
        st = Study.create(self.cfg, self.sp, {"name": name, "stage": "B", "sampler": "sobol", "active": ACTIVE, "trials": trials, "seed": 4, "variants": 0})
        st.run(runner, workers=1, out=io.StringIO())
        return st


class PlateauTests(Case):
    def test_calmar_alone_picks_the_spike_and_the_neighbourhood_picks_the_plateau(self):
        st = self.study()
        recs = read_records(st.trials_path)
        fin = robust.finalists(recs, 30)
        spike = [f for f in fin if f["f1"] >= 0.25]
        plateau = [f for f in fin if abs(f["f1"] - 0.15) < 1e-9]
        self.assertTrue(spike and plateau, "both regions must reach the front")
        self.assertEqual(max(fin, key=lambda f: f["calmar"])["f1"], 0.25)  # the peak wins on Calmar
        pool = EvalPool(SpikeRunner, 1)
        doc = robust.run(st, pool, self.cfg, 30)
        by = {s["trialId"]: s for s in doc["scores"]}
        self.assertTrue(all(not by[f["trialId"]]["robust"] for f in spike if f["trialId"] in by))
        self.assertTrue(any(by[f["trialId"]]["robust"] for f in plateau if f["trialId"] in by))
        chosen = by[doc["selected"]]
        self.assertAlmostEqual(chosen["f1"], 0.15)
        self.assertEqual(doc["decision"], "candidate")

    def test_nothing_robust_means_keep_the_defaults(self):
        self.assertIsNone(robust.select([{"robust": False, "calmar": 3, "ulcer": 1}]))
        self.assertEqual(robust.select([{"robust": True, "calmar": 1.0, "ulcer": 5, "k": 1}, {"robust": True, "calmar": 1.0, "ulcer": 3, "k": 2}, {"robust": False, "calmar": 9, "ulcer": 0}])["k"], 2)

    def test_robust_runs_resume_without_repeating_finished_neighbours(self):
        st = self.study()
        made = []

        def factory():
            made.append(SpikeRunner())
            return made[-1]

        robust.run(st, EvalPool(factory, 1), self.cfg, 5)
        first = made[0].calls
        robust.run(st, EvalPool(factory, 1), self.cfg, 5)
        self.assertEqual(made[1].calls, 0)
        self.assertGreater(first, 0)


class GateTests(Case):
    def evidence(self, runner=FakeRunner, trials=40):
        st = self.study(runner, trials)
        recs = [r for r in read_records(st.trials_path) if r["feasible"]]
        rec = max(recs, key=lambda r: r["objectives"][0])
        led = ledger_mod.Ledger(Path(self.cfg["paths"]["data"]) / "ledger" / "l.json", 200, 0.3)
        led.record("p", "B", 40, 12)
        ev = gate.collect(st, EvalPool(runner, 1), self.cfg, rec, led)
        return st, rec, ev

    def test_evidence_covers_the_candidate_the_default_the_stresses_neighbours_grid_and_statistics(self):
        st, rec, ev = self.evidence()
        self.assertEqual(set(ev["stress"]), {"slippage2", "charges1.3", "writeOff100", "drop5pct", "otherUniverse"})
        self.assertGreaterEqual(len(ev["neighbours"]), 10)
        self.assertEqual(ev["grid"]["n"], 5)
        self.assertEqual(len(ev["grid"]["cagr"]), 5)
        self.assertEqual(ev["stats"]["dsr"]["effectiveN"], 12)
        self.assertTrue(0 <= ev["stats"]["pbo"]["pbo"] <= 1 and len(ev["stats"]["pbo"]["logits"]) == 12870)
        self.assertEqual(ev["candidate"]["trialId"], rec["trialId"])
        self.assertEqual(find_trial(self.cfg, rec["trialId"])[0], "p")
        self.assertTrue(all(d["new"] != d["old"] for d in ev["diff"]))
        verdict = gate.checks(ev, self.cfg)
        self.assertEqual(set(verdict["checks"]), {"feasible", "deflatedSharpe", "pbo", "outOfSample", "neighbourhood", "stressRanking", "regimeAndStress", "spa"})
        aud = gate.audit(ev, self.cfg)
        self.assertIn("surveillance", aud["flagged"])  # the fake run does not model surveillance
        self.assertTrue(aud["notAssessed"])

    def test_save_writes_the_files_the_report_reads(self):
        st, rec, ev = self.evidence(trials=30)
        folder = Path(self.cfg["paths"]["data"]) / "candidates" / rec["trialId"]
        gate.save(folder, ev, gate.checks(ev, self.cfg), gate.audit(ev, self.cfg))
        for name in ("evidence.json", "gate.json", "series.parquet", "returns.parquet"):
            self.assertTrue((folder / name).exists(), name)
        cols = set(pd.read_parquet(folder / "series.parquet").columns)
        self.assertTrue({"date", "candidate.nav", "default.nav", "benchmark", "candidate.regime"} <= cols)

    def test_known_overfit_fails_the_gate(self):
        """500 noise strategies: the best one has PBO near one half and a deflated Sharpe under 0.95, so the gate refuses it."""
        m = noise()
        sh = stats.trial_sharpes(m)
        best = m.columns[int(np.argmax(sh))]
        pbo = stats.pbo_logits(m)
        dsr = stats.dsr_curve(m[best].to_numpy(), sh, [500, 1000])
        good = {"status": "ok", "feasible": True, "objectives": [0.12, 0.1], "metrics": {"cagr": 0.12, "maxDrawdown": -0.1, "ulcerIndex": 3.0, "fills": 400, "foldCagr": [0.1, 0.12, 0.14]}}
        det = {"stressWindows": {}, "regimes": {}, "vanished": {"exits": 0, "writtenOffInr": 0.0}, "surveillanceModelled": True, "realism": {}}
        ev = {"candidate": good, "default": good, "neighbours": [{**good, "kind": "single", "changed": ["x"]}] * 10, "detail": {"candidate": det, "default": det},
              "stress": {t: {"candidate": good, "default": good} for t in ("slippage2", "writeOff100")},
              "stats": {"pbo": {"pbo": pbo["pbo"], "trials": 500}, "dsr": {"atN": dsr[0]["dsr"], "at2N": dsr[1]["dsr"], "effectiveN": 500}, "spa": {"p": 0.6}}}
        v = gate.checks(ev, self.cfg)
        self.assertFalse(v["passed"])
        self.assertIn("deflatedSharpe", v["failed"])
        self.assertIn("adopt for feasibility", v["label"])
        self.assertFalse(v["checks"]["spa"]["passed"])

    def test_a_robust_good_candidate_passes_and_a_cliff_or_bound_is_flagged_by_the_audit(self):
        good = {"status": "ok", "feasible": True, "objectives": [0.15, 0.08], "metrics": {"cagr": 0.15, "maxDrawdown": -0.08, "ulcerIndex": 3.0, "fills": 400, "foldCagr": [0.12, 0.15, 0.18]}}
        det = {"stressWindows": {"w": {"maxDrawdown": -0.1}}, "regimes": {"BEAR": {"maxDrawdown": -0.1}}, "vanished": {"exits": 0, "writtenOffInr": 0.0}, "surveillanceModelled": True,
               "realism": {"bands": True}}
        weak = {**good, "objectives": [0.05, 0.1], "metrics": {**good["metrics"], "cagr": 0.05}}
        ev = {"candidate": good, "default": weak, "neighbours": [{**good, "kind": "single", "changed": ["x"]}] * 8 + [{**good, "kind": "multi", "changed": ["x", "y"]}] * 2,
              "detail": {"candidate": det, "default": det}, "stress": {t: {"candidate": good, "default": weak} for t in ("slippage2", "writeOff100")},
              "stats": {"pbo": {"pbo": 0.1, "trials": 300}, "dsr": {"atN": 0.99, "at2N": 0.97, "effectiveN": 100}, "spa": {"p": 0.03}},
              "bounds": [{"name": "a", "low": 1, "high": 5, "value": 5}, {"name": "b", "low": 0, "high": 1, "value": 0.5}]}
        v = gate.checks(ev, self.cfg)
        self.assertTrue(v["passed"], v["failed"])
        self.assertIsNone(v["label"])
        cliff = {**good, "kind": "single", "changed": ["z"], "metrics": {**good["metrics"], "cagr": 0.05}}
        a = gate.audit({**ev, "neighbours": ev["neighbours"] + [cliff]}, self.cfg)
        self.assertEqual(a["items"]["parametersAtABound"]["names"], ["a"])
        self.assertEqual(a["items"]["cliffs"]["params"], [["z"]])
        self.assertFalse(a["passed"])

    def test_a_default_that_trades_nothing_is_still_run_to_the_end(self):
        job = {"values": self.sp.complete(self.sp.defaults), "noAbort": True}
        self.assertEqual(FakeRunner().run(job)["status"], "ok")


class StressTests(unittest.TestCase):
    def test_costs_are_scaled_and_the_original_is_untouched(self):
        *_, sp = load_space()
        risk, _, _ = sp.decode(sp.defaults)
        before = risk["costs"]["slippageBpsPerSide"]["MidCap"], risk["costs"]["sttPct"], risk["costs"]["brokerage"]["pct"], risk["costs"]["gstPct"]
        out = objective.apply_stress(risk, {"slippageMult": 2.0, "chargesMult": 1.3})
        c = out["costs"]
        self.assertEqual(c["slippageBpsPerSide"]["MidCap"], before[0] * 2)
        self.assertAlmostEqual(c["sttPct"], before[1] * 1.3)
        self.assertAlmostEqual(c["brokerage"]["pct"], before[2] * 1.3)
        self.assertEqual(c["gstPct"], before[3])  # a tax rate on the fees, not a fee
        self.assertEqual((risk["costs"]["slippageBpsPerSide"]["MidCap"], risk["costs"]["sttPct"]), before[:2])
        self.assertIs(objective.apply_stress(risk, None), risk)


if __name__ == "__main__":
    unittest.main()


class CandidateReportTests(GateTests):
    test_evidence_covers_the_candidate_the_default_the_stresses_neighbours_grid_and_statistics = None
    test_save_writes_the_files_the_report_reads = None
    test_known_overfit_fails_the_gate = None
    test_a_robust_good_candidate_passes_and_a_cliff_or_bound_is_flagged_by_the_audit = None
    test_a_default_that_trades_nothing_is_still_run_to_the_end = None

    def test_the_candidate_report_has_charts_11_to_24_offline_and_no_holdout_data(self):
        from hpo.viz import candidate
        st, rec, ev = self.evidence(trials=40)
        folder = Path(self.cfg["paths"]["data"]) / "candidates" / rec["trialId"]
        verdict = gate.checks(ev, self.cfg)
        gate.save(folder, ev, verdict, gate.audit(ev, self.cfg))
        text = candidate.candidate_report(folder, self.cfg).read_text(encoding="utf-8")
        import re
        for i in range(11, 25):
            self.assertIn(f'id="c{i}"', text)
        for i in (11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23):
            self.assertIn(f'id="plot-c{i}"', text, f"chart {i}")
        self.assertNotIn('id="plot-c24"', text)
        own = text.split("</main>")[0] + text.split("</main>")[1].split("</script>", 1)[1]
        self.assertIsNone(re.search(r'(?:src|href)="https?://|@import|url\(http', own))
        self.assertIn("locked: not scored", text)
        self.assertIn("HPO is unlikely to yield", text)  # the honest-edge statement
        self.assertNotIn(str(self.cfg["paths"]["data"]), text)
