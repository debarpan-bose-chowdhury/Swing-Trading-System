"""Objectives, constraints, aborts and failure mapping; parity of one fold-sliced run with the backtest's own evaluate_config on a synthetic world."""

import json
import math
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from backtest import api, walkforward, world
from hpo import objective
from tests.backtest.helpers import repo_config
from tests.backtest.test_targets import World
from tests.hpo.fakes import REPO, make_settings
from tests import fixtures

CFG = make_settings("hpo-data")


def series(n=400, mu=0.0004, sd=0.01, seed=1, start="2020-01-01"):
    days = [d.date().isoformat() for d in pd.bdate_range(start, periods=n)]
    return pd.Series(np.random.default_rng(seed).normal(mu, sd, n), index=days)


class ScoreTests(unittest.TestCase):
    def test_cvar_is_the_mean_of_the_worst_share(self):
        self.assertAlmostEqual(objective.cvar_worst([0.2, -0.1, 0.1, 0.0, 0.3, -0.3, 0.05, 0.15, 0.25, 0.01], 0.3), (-0.3 - 0.1 + 0.0) / 3)
        self.assertEqual(objective.cvar_worst([0.1, 0.2], 0.1), 0.1)  # at least one fold

    def test_a_fold_is_a_window_of_the_one_run_and_equals_the_compounded_slice(self):
        r = series()
        folds = [(r.index[50], r.index[149]), (r.index[150], r.index[249])]
        fills = pd.DataFrame({"trade_date": [r.index[60], r.index[70], r.index[160]]})
        stats = objective.fold_stats(r, folds, fills, 6.5, 252)
        for (a, b), s in zip(folds, stats):
            sl = r[(r.index >= a) & (r.index <= b)]
            self.assertAlmostEqual(s["cagr"], float((1 + sl).prod() ** (252 / len(sl)) - 1), places=5)  # perf() rounds to 6 decimals
            self.assertEqual(s["days"], 100)
        self.assertEqual([s["fills"] for s in stats], [2, 1])
        self.assertAlmostEqual(stats[0]["fillsPerYear"], 2 / (100 / 252))

    def _score(self, r, fills=400, exposure=0.7, **limits):
        cfg = json.loads(json.dumps(CFG))
        cfg["constraints"].update(limits)
        nav = pd.DataFrame({"positions_value": [exposure * 100.0] * 10, "nav": [100.0] * 10})
        folds = [(r.index[50], r.index[199]), (r.index[200], r.index[349])]
        f = pd.DataFrame({"trade_date": [r.index[60 + i % 200] for i in range(fills)]})
        return objective.score(r, nav, f, api.perf((1 + r).cumprod().pct_change().dropna(), 6.5, days=252) | {"maxDrawdown": api.perf(r, 6.5, days=252)["maxDrawdown"]}, folds, cfg, 6.5, 252)

    def test_constraints_use_the_value_le_zero_is_feasible_convention(self):
        ok = self._score(series())
        self.assertTrue(objective.feasible(ok["constraints"]), ok["constraints"])
        self.assertEqual(ok["values"][1], -ok["metrics"]["maxDrawdown"])
        self.assertGreater(self._score(series(), fills=100)["constraints"]["min_fills"], 0)
        self.assertGreater(self._score(series(), exposure=0.2)["constraints"]["min_exposure"], 0)
        self.assertGreater(self._score(series(sd=0.05, mu=-0.002))["constraints"]["dd_cap"], 0)
        self.assertGreater(self._score(series(), fills=20)["constraints"]["fills_per_fold_year"], 0)

    def test_the_monitor_aborts_on_drawdown_and_on_no_fills_and_never_on_performance(self):
        mon = objective.make_monitor(CFG["constraints"], 252)
        rows = [{"nav": 100.0 * (1 + 0.001 * i), "date": f"d{i}"} for i in range(50)]
        mon(rows, 5)  # rising: fine
        with self.assertRaises(objective.AbortRun) as a:
            mon(rows + [{"nav": rows[-1]["nav"] * (1 + CFG["constraints"]["abortDrawdown"] - 0.05), "date": "dX"}], 5)  # 5 points beyond the configured abort
        self.assertEqual((a.exception.reason, a.exception.asof), ("drawdown", "dX"))
        flat = [{"nav": 100.0, "date": f"d{i}"} for i in range(3 * 252 + 1)]
        with self.assertRaises(objective.AbortRun) as b:
            mon(flat, 0)
        self.assertEqual(b.exception.reason, "no fills")
        mon(flat[:600], 0)  # under three years without a fill is still allowed
        mon(flat, 1)

    def test_a_placeholder_is_infeasible_and_marked(self):
        p = objective.placeholder("invalid", error="x")
        self.assertFalse(objective.feasible(p["constraints"]))
        self.assertEqual((p["constraints"]["valid"], p["constraints"]["aborted"]), (1.0, 0.0))
        self.assertEqual(objective.placeholder("aborted")["constraints"]["aborted"], 1.0)

    def test_non_finite_values_are_detected(self):
        o = {"values": [0.1, 0.2], "constraints": {"a": 0.0}}
        self.assertTrue(objective.finite(o))
        self.assertFalse(objective.finite({**o, "values": [math.nan, 0.2]}))


class EngineCase(World):
    """The real engine on the synthetic 900-day world, windows scaled to it (the shipped 5y/1y folds need 13 years)."""

    data_dir = "app/data"

    def setUp(self):
        super().setUp()
        fixtures.copy_app_config()
        Path("app/config/nse_calendar.json").write_text(json.dumps({"holidays": ["2021-01-01"], "specialSessions": []}))
        self.bt = repo_config()
        self.bt["overrides"]["risk"] = {"sizing": {"minNewOrderInr": 3000, "minAdjustmentInr": 1500}}
        self.bt["tax"]["confirmed"] = True
        self.bt["capital"]["inr"] = 100000  # the abort tests need an account too small to trade at a 10,000 minimum order: pinned here so they do not move with the shipped capital
        self.w = world.World.build(self.bt)
        wf = dict(self.bt["walkforward"], trainYears=1, testYears=1, purgeDays=10)
        self.win = walkforward.Windows(list(self.w.data.index.Date), self.days[300], wf, 1, 0)
        self.folds = [(self.days[400], self.days[470]), (self.days[471], self.days[540])]
        self.cfg = make_settings(str(self.root / "hpo-data"))
        self.runner = objective.BacktestRunner(self.cfg, world=self.w, windows=self.win, folds=self.folds, schema_path=str(REPO / "hpo/schema/parameters.schema.json"))


class EngineTests(EngineCase):
    def test_the_default_point_is_the_world_and_the_run_equals_evaluate_config(self):
        out = self.runner.run({"values": self.runner.space.defaults})
        ev = api.evaluate_config(self.w, self.w.risk, self.w.analyst, *self.runner.span, haircut=0.5)
        self.assertEqual(out["status"], "ok")
        pd.testing.assert_series_equal(out["returns"], ev.returns)
        self.assertEqual(out["metrics"]["fills"], len(ev.fills))
        self.assertEqual(out["metrics"]["cagr"], ev.metrics["cagr"])
        self.assertEqual(out["values"][1], -ev.metrics["maxDrawdown"])
        folds = objective.fold_stats(ev.returns, self.folds, ev.fills, self.w.risk["evaluator"]["riskFreeRatePct"], 252)
        self.assertEqual(out["values"][0], objective.cvar_worst([f["cagr"] for f in folds], 0.30))
        self.assertGreater(out["metrics"]["fills"], 5)
        self.assertAlmostEqual(sum(out["regimeShare"].values()), 1.0)

    def test_the_same_point_gives_the_same_outcome(self):
        a, b = (self.runner.run({"values": self.runner.space.defaults}) for _ in range(2))
        pd.testing.assert_series_equal(a["returns"], b["returns"])
        self.assertEqual((a["values"], a["constraints"]), (b["values"], b["constraints"]))

    def test_a_run_never_reads_the_holdout(self):
        self.assertLess(self.runner.span[1], self.win.holdout_start)
        with self.assertRaises(walkforward.HoldoutRead):
            self.win.check_tuning(self.runner.span[0], self.win.last)

    def test_identity_carries_what_a_cached_result_depends_on(self):
        ident = self.runner.identity()
        self.assertEqual(ident["dataHash"], self.w.data.data_hash())
        self.assertEqual(ident["span"], list(self.runner.span))
        self.assertEqual(set(ident), {"dataHash", "codeSha", "baseConfig", "span", "folds", "writeOff", "schemaVersion"})

    def test_a_rejected_point_never_simulates(self):
        with patch.object(api, "evaluate_config", side_effect=AssertionError("must not run")):
            out = self.runner.run({"values": {"no.such": 1}})
        self.assertEqual(out["status"], "invalid")

    def test_a_run_with_no_fills_is_aborted_as_infeasible_not_as_poor(self):
        cfg = make_settings(str(self.root / "x"), constraints__abortNoFillYears=0.2)
        runner = objective.BacktestRunner(cfg, world=self.w, windows=self.win, folds=self.folds, schema_path=str(REPO / "hpo/schema/parameters.schema.json"))
        no_trade = {**runner.space.defaults, "risk.sizing.minNewOrderInr": 25000, "risk.sizing.minAdjRatio": 0.4}
        out = runner.run({"values": no_trade})
        self.assertEqual((out["status"], out["attrs"]["abortReason"]), ("aborted", "no fills"))
        self.assertEqual(out["constraints"]["aborted"], 1.0)

    def test_an_exception_or_a_non_finite_result_is_a_fail(self):
        with patch.object(api, "evaluate_config", side_effect=RuntimeError("boom")):
            out = self.runner.run({"values": self.runner.space.defaults})
        self.assertEqual((out["status"], out["error"]), ("fail", "RuntimeError: boom"))
        real = api.evaluate_config

        def nan_run(*a, **k):
            ev = real(*a, **k)
            ev.metrics["maxDrawdown"] = float("nan")
            return ev

        with patch.object(api, "evaluate_config", nan_run):
            self.assertEqual(self.runner.run({"values": self.runner.space.defaults})["status"], "fail")


if __name__ == "__main__":
    unittest.main()
