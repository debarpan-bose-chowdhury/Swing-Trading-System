"""Parameter Exposure S1 on the backtest side: carry-over inheritance (H9), the shared purge floor (H10), the public API."""

import copy
import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from app.risk import common as risk_common
from backtest import api, config, params, replay, tax, walkforward, world
from backtest.targets import Targets, TargetsCache
from tests.backtest.helpers import repo_config
from tests.backtest.test_params_walkforward import schema
from tests.backtest.test_phase4 import SingleRun

RISK = risk_common.load_config("run")


class CarryOver(unittest.TestCase):
    def test_null_inherits_the_live_shadow_value_and_an_integer_overrides_it(self):
        cfg = repo_config()
        self.assertIsNone(cfg["fill"]["carryOverDays"])
        self.assertEqual(config.carry_over_days(cfg, RISK), 7)
        risk = copy.deepcopy(RISK)
        risk["shadow"]["carryOverDays"] = 3
        self.assertEqual(config.carry_over_days(cfg, risk), 3)
        cfg["fill"]["carryOverDays"] = 5
        self.assertEqual(config.carry_over_days(cfg, risk), 5)

    def test_validation_accepts_null_or_a_non_negative_integer(self):
        for ok in (None, 0, 7):
            cfg = repo_config()
            cfg["fill"]["carryOverDays"] = ok
            config.validate(cfg)
        for bad in (-1, 1.5, "7", True):
            cfg = repo_config()
            cfg["fill"]["carryOverDays"] = bad
            with self.assertRaises(ValueError):
                config.validate(cfg)


class PurgeFloor(unittest.TestCase):
    def test_there_is_one_floor_and_both_places_read_it(self):
        self.assertFalse(hasattr(params, "MIN_PURGE"))
        s = schema()
        self.assertEqual(s.required_purge(), 250)  # the slowest allowed SMA is above the floor
        saved, cfg = config.MIN_PURGE_DAYS, repo_config()
        try:
            config.MIN_PURGE_DAYS = 300
            self.assertEqual(s.required_purge(), 300)
            cfg["walkforward"]["purgeDays"] = 250
            with self.assertRaises(ValueError):
                config.validate(cfg)
        finally:
            config.MIN_PURGE_DAYS = saved
        self.assertEqual(config.MIN_PURGE_DAYS, 168)

    def test_warm_up_follows_regime_unknown_extra(self):
        s = schema()
        self.assertEqual(s.warmup_rows(), 250 + 9)
        s.base["analyst"] = {**s.base["analyst"], "regime": {**s.base["analyst"]["regime"], "unknownExtra": 20}}
        self.assertEqual(s.warmup_rows(), 250 + 20)


class Api(SingleRun):
    def setUp(self):
        super().setUp()
        self.w = world.World.build(self.bt)
        self.a, self.b = self.days[400], self.days[470]

    def inline(self, start, end, capital=None):
        """The pipeline backtest.run.evaluate and trials.Session.evaluate used before they called the API."""
        cfg, w = self.bt, self.w
        result = replay.simulate(w.data, w.targets, w.risk, start, end, cfg["capital"]["inr"] if capital is None else capital, w.surveillance,
                                 carry_over_days=7, dividends=w.dividends, restart_after=config.restart_after(cfg))
        taxes = tax.assess(tax.lots(result.fills), cfg["tax"]["schedule"])
        return result, taxes, tax.post_tax_curve(result.nav, taxes)

    def test_same_nav_fills_taxes_and_post_tax_returns_as_the_inline_pipeline(self):
        ev = api.evaluate_config(self.w, self.w.risk, self.w.analyst, self.a, self.b)
        result, taxes, post = self.inline(self.a, self.b)
        pd.testing.assert_frame_equal(ev.nav, result.nav)
        pd.testing.assert_frame_equal(ev.fills, result.fills)
        self.assertEqual(ev.taxes, taxes)
        pd.testing.assert_series_equal(ev.post_tax_nav, post)
        pd.testing.assert_series_equal(ev.returns, post.pct_change().dropna())
        self.assertGreater(len(ev.fills), 5)

    def test_run_evaluate_and_the_api_report_the_same_figures(self):
        rep = run_evaluate(self.bt, self.w, self.a, self.b)
        ev = api.evaluate_config(self.w, self.w.risk, self.w.analyst, self.a, self.b)
        self.assertEqual(rep["postTax"], ev.metrics)
        self.assertEqual((rep["configHash"], rep["dataHash"]), (ev.hashes["config"], ev.hashes["data"]))

    def test_hashes_capital_and_a_different_config(self):
        ev = api.evaluate_config(self.w, self.w.risk, self.w.analyst, self.a, self.b)
        self.assertEqual(set(ev.hashes), {"config", "data", "code"})
        self.assertEqual(ev.hashes["data"], self.w.data.data_hash())
        small = api.evaluate_config(self.w, self.w.risk, self.w.analyst, self.a, self.b, capital=300000.0)
        self.assertNotEqual(small.nav.nav.iloc[0], ev.nav.nav.iloc[0])
        risk = copy.deepcopy(self.w.risk)
        risk["stops"]["atrMultiplier"] = 2.5
        other = api.evaluate_config(self.w, risk, self.w.analyst, self.a, self.b)
        self.assertNotEqual(other.hashes["config"], ev.hashes["config"])

    def test_an_empty_window_is_an_error(self):
        with self.assertRaises(ValueError):
            api.evaluate_config(self.w, self.w.risk, self.w.analyst, "2090-01-01", "2090-02-01")

    def test_the_world_targets_are_reused_and_a_new_analyst_config_builds_once(self):
        cache = api.targets_cache(self.w)
        self.assertIs(cache.get(self.w.analyst), self.w.targets)
        analyst = copy.deepcopy(self.w.analyst)
        analyst["strategies"]["BULL"]["LargeCap"]["top_n"] = 1
        t = cache.get(analyst)
        self.assertIsNot(t, self.w.targets)
        self.assertIs(cache.get(analyst), t)

    def test_holdout_guard_is_the_one_shot_marker(self):
        dates = self.days
        wf = dict(self.bt["walkforward"], trainYears=1, testYears=1, purgeDays=10)
        win = walkforward.Windows(dates, dates[300], wf, 1, 0)
        marker = Path(tempfile.mkdtemp()) / "holdout.json"
        first = api.holdout_guard(win, marker, "k1")
        self.assertEqual(first, (win.holdout_start, win.last))
        self.assertEqual(api.holdout_guard(win, marker, "k1"), first)
        with self.assertRaises(api.HoldoutRead):
            api.holdout_guard(win, marker, "k2")

    def test_build_world_applies_a_validated_override(self):
        Path("backtest/config").mkdir(parents=True, exist_ok=True)
        Path("backtest/config/backtest.json").write_text(json.dumps(self.bt), encoding="utf-8")
        with self.assertRaises(ValueError):
            api.build_world({"fill": {"carryOverDays": -1}})


class TargetsCacheTests(SingleRun):
    def test_size_is_configurable_and_the_oldest_entry_goes_first(self):
        w = world.World.build(self.bt)
        cache = TargetsCache(w.data, 2)
        variants = []
        for n in (1, 2, 3):
            a = copy.deepcopy(w.analyst)
            a["strategies"]["BULL"]["LargeCap"]["top_n"] = n
            variants.append(a)
        built = [cache.get(a) for a in variants]
        self.assertEqual(len(cache._items), 2)
        self.assertIs(cache.get(variants[2]), built[2])
        self.assertIsNot(cache.get(variants[0]), built[0])  # evicted, rebuilt
        with self.assertRaises(ValueError):
            TargetsCache(w.data, 0)
        self.assertIsInstance(built[0], Targets)


def run_evaluate(cfg, w, a, b):
    from backtest import run
    return run.evaluate(cfg, w, a, b)


if __name__ == "__main__":
    unittest.main()
