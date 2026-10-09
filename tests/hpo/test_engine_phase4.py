"""Phase 4 on the real engine (synthetic world): a stressed run costs more, detail carries the curves, dropping names changes the universe, a baseline never aborts."""

import unittest

import pandas as pd

from backtest import api
from hpo import objective
from tests.hpo.test_objective import EngineCase


class EngineStressTests(EngineCase):
    def run_job(self, **kw):
        return self.runner.run({"values": self.runner.space.defaults, **kw})

    def test_detail_carries_the_curves_regimes_stress_windows_and_trade_profile(self):
        out = self.run_job(detail=True)
        d = out["detail"]
        self.assertEqual(out["status"], "ok")
        self.assertGreater(len(d["nav"]), len(out["returns"]))  # the NAV curve has one more point than the returns derived from it
        self.assertTrue(set(d["regimes"]) <= {"BULL", "TREND", "WEAK", "BEAR", "Unknown"})
        self.assertAlmostEqual(sum(v["share"] for v in d["regimes"].values()), 1.0)
        self.assertGreater(sum(d["profile"]["fillsPerYear"].values()), 5)
        self.assertIn("exits", d["vanished"])
        self.assertFalse(d["surveillanceModelled"])
        self.assertTrue(d["exposure"].between(0, 1.5).all())
        for w in d["stressWindows"].values():
            self.assertIn("spans", w)

    def test_stressed_costs_lower_the_result_and_a_write_off_changes_it_only_when_a_name_vanished(self):
        base = self.run_job()
        slip = self.run_job(stress={"slippageMult": 3.0, "chargesMult": 2.0})
        self.assertLess(slip["metrics"]["cagr"], base["metrics"]["cagr"])
        self.assertEqual(slip["metrics"]["fills"], base["metrics"]["fills"])
        total = self.run_job(stress={"writeOff": 1.0})
        self.assertLessEqual(total["metrics"]["cagr"], base["metrics"]["cagr"] + 1e-12)

    def test_dropping_names_builds_a_smaller_universe_and_leaves_the_original_world_alone(self):
        before = sorted(self.w.data.series)
        w2 = api.drop_names(self.w, 0.3, seed=1)
        self.assertEqual(sorted(self.w.data.series), before)
        self.assertEqual(len(w2.data.series), len(before) - round(0.3 * len(before)))
        self.assertEqual(sorted(api.drop_names(self.w, 0.3, seed=1).data.series), sorted(w2.data.series))  # seeded
        out = self.run_job(stress={"dropNames": 0.3, "seed": 1})
        self.assertEqual(out["status"], "ok")
        self.assertNotEqual(out["metrics"]["fills"], self.run_job()["metrics"]["fills"])

    def test_a_baseline_that_never_trades_is_not_aborted_when_asked_to_run_to_the_end(self):
        cfg_runner = objective.BacktestRunner(
            __import__("tests.hpo.fakes", fromlist=["make_settings"]).make_settings(str(self.root / "y"), constraints__abortNoFillYears=0.2),
            world=self.w, windows=self.win, folds=self.folds, schema_path=str(__import__("tests.hpo.fakes", fromlist=["REPO"]).REPO / "hpo/schema/parameters.schema.json"))
        no_trade = {**cfg_runner.space.defaults, "risk.sizing.minNewOrderInr": 25000, "risk.sizing.minAdjRatio": 0.4}
        self.assertEqual(cfg_runner.run({"values": no_trade})["status"], "aborted")
        out = cfg_runner.run({"values": no_trade, "noAbort": True})
        self.assertEqual((out["status"], out["metrics"]["fills"]), ("ok", 0))


class EngineHoldoutTests(EngineCase):
    def job(self, key, marker):
        return {"holdout": True, "values": self.runner.space.defaults, "default": self.runner.space.defaults, "marker": str(marker), "paramsKey": key}

    def test_the_real_holdout_door_runs_the_holdout_window_once_for_one_set(self):
        from pathlib import Path
        marker = Path(self.root) / "hold" / "holdout.marker"
        out = self.runner.run(self.job("setA", marker))
        self.assertEqual(out["window"], [self.win.holdout_start, self.win.last])
        self.assertGreater(self.win.holdout_start, self.runner.span[1])  # no search run reaches it
        self.assertEqual(set(out["candidate"]), {"metrics", "fills", "years", "depth", "returns"})
        self.assertEqual(out["candidate"]["returns"].index[0] >= self.win.holdout_start, True)
        self.runner.run(self.job("setA", marker))  # the guard allows the same set (hpo itself refuses a second look)
        from backtest import api
        with self.assertRaises(api.HoldoutRead):
            self.runner.run(self.job("setB", marker))

    def test_a_replay_returns_the_twr_index_from_the_start_date(self):
        out = self.runner.run({"replay": True, "values": self.runner.space.defaults, "start": self.days[400], "end": self.days[440]})
        self.assertEqual(out["twr"].index[0], self.days[400])
        self.assertGreater(len(out["twr"]), 20)


if __name__ == "__main__":
    unittest.main()
