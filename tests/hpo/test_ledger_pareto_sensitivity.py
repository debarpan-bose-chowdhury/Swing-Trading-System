"""Pareto and hypervolume, the effective-N ledger, and sensitivity on a landscape whose important parameters are known."""

import io
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from hpo import ledger, pareto, sensitivity
from hpo.errors import Failed, Refusal
from hpo.study import Study, read_records
from tests.hpo.fakes import FakeRunner, make_settings
from tests.hpo.test_space import load_space


class ParetoTests(unittest.TestCase):
    def test_front_keeps_the_non_dominated_points_only(self):
        pts = [(0.10, 0.20), (0.12, 0.25), (0.08, 0.15), (0.09, 0.30), (0.12, 0.25), (0.05, 0.15)]
        self.assertEqual(pareto.front(pts), [0, 1, 2, 4])  # (0.09, 0.30) and (0.05, 0.15) are dominated; duplicates both stay

    def test_hypervolume_is_the_dominated_area_inside_the_reference(self):
        ref = (0.0, 0.5)
        self.assertAlmostEqual(pareto.hypervolume([(0.10, 0.20)], ref), 0.10 * 0.30)
        self.assertAlmostEqual(pareto.hypervolume([(0.10, 0.20), (0.05, 0.10)], ref), 0.10 * 0.30 + 0.05 * 0.10)
        self.assertEqual(pareto.hypervolume([(-0.1, 0.2), (0.1, 0.6)], ref), 0.0)  # outside the corner
        more = pareto.hypervolume([(0.10, 0.20), (0.05, 0.10), (0.07, 0.25)], ref)
        self.assertAlmostEqual(more, 0.10 * 0.30 + 0.05 * 0.10)  # a dominated point adds nothing


class LedgerTests(unittest.TestCase):
    def test_near_duplicates_merge_and_independent_series_do_not(self):
        rng = np.random.default_rng(0)
        base = rng.normal(0, 0.01, (500, 5))
        dup = pd.DataFrame(np.column_stack([base, base[:, 0] + rng.normal(0, 1e-5, 500), base[:, 1] * 1.0001]))
        self.assertEqual(ledger.effective_n(dup, 0.5), 5)
        self.assertEqual(ledger.effective_n(pd.DataFrame(base), 0.5), 5)
        self.assertEqual(ledger.effective_n(pd.DataFrame(base[:, :1]), 0.5), 1)

    def test_series_that_never_trade_form_one_cluster(self):
        rng = np.random.default_rng(1)
        x = pd.DataFrame(np.column_stack([np.zeros((300, 4)), rng.normal(0, 0.01, (300, 2))]))
        self.assertEqual(ledger.effective_n(x, 0.5), 3)

    def test_effective_n_never_decreases_and_the_cap_refuses_and_logs_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            led = ledger.Ledger(Path(tmp) / "l.json", cap=200, default_ratio=0.3)
            led.record("a", "A", 100, 40)
            led.record("a", "A", 150, 30)  # a lower recount never lowers it
            self.assertEqual((led.doc["studies"]["a"]["raw"], led.doc["studies"]["a"]["effective"]), (150, 40))
            self.assertAlmostEqual(led.ratio(), 40 / 150)
            self.assertTrue(led.check("b", 300)["ok"])  # about 80 more: 120 <= 200
            with self.assertRaisesRegex(Refusal, "cap"):
                led.check("b", 900)  # about 240 more
            self.assertFalse(led.check("b", 900, override="needed")["ok"])
            self.assertEqual(ledger.Ledger(Path(tmp) / "l.json", 200, 0.3).doc["overrides"][0]["reason"], "needed")

    def test_before_any_history_the_default_ratio_plans_the_study(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(ledger.Ledger(Path(tmp) / "l.json", 200, 0.3).plan("x", 100)["expectedNew"], 30)


class SensitivityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg0, cls.risk, cls.analyst, cls.sp = load_space()

    def test_the_parameters_that_drive_the_landscape_rank_first_and_the_rest_freeze(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_settings(str(Path(tmp) / "data"), sensitivity__maxActive=6)
            relevant = ["risk.stops.atrMultiplier", "risk.sizing.riskPerPositionPct", "risk.sizing.minNewOrderInr"]
            noise = ["risk.sizing.nameCapPct.LargeCap", "risk.sizing.nameCapPct.MidCap", "risk.sizing.noTradeBand.relative", "risk.sizing.noTradeBand.floorPct",
                     "risk.liquidity.advDays", "risk.heat.capPct", "risk.tax.deferral.minGainPct"]
            noise = [n for n in noise if n in self.sp.dims and self.sp.dims[n].cls == "tunable"]
            st = Study.create(cfg, self.sp, {"name": "sa", "stage": "A", "sampler": "sobol", "active": relevant + noise, "trials": 160, "seed": 5, "variants": 0})
            st.run(FakeRunner, workers=1, out=io.StringIO())
            res = sensitivity.analyse(self.sp, st.spec, read_records(st.trials_path), cfg)
            top = [r["name"] for r in res["dims"][:3]]
            self.assertEqual(set(top), set(relevant), [(r["name"], round(r["combined"], 3)) for r in res["dims"]])
            self.assertTrue(set(relevant) <= set(res["keep"]))
            self.assertTrue(set(noise) & set(res["freeze"]))
            self.assertEqual(res["proposedStudy"]["active"], res["keep"])
            sp_atr = next(r for r in res["dims"] if r["name"] == "risk.stops.atrMultiplier")
            self.assertGreater(abs(sp_atr["spearmanF1"]) + sp_atr["importanceF1"], 0.1)

    def test_too_few_scored_trials_are_refused(self):
        with self.assertRaises(Failed):
            sensitivity.analyse(self.sp, {"name": "x", "active": ["risk.stops.atrMultiplier"]}, [], self.cfg0)


if __name__ == "__main__":
    unittest.main()
