import json
import random
import unittest
from pathlib import Path

import pandas as pd

from app.analyst import common as analyst_common
from app.risk import common as risk_common
from backtest import params, walkforward

REPO = Path(__file__).resolve().parents[2]
RISK, ANALYST = risk_common.load_config("run"), analyst_common.load_config()  # read before any test changes the working directory
BT = json.loads((REPO / "backtest/config/backtest.json").read_text())


def schema(**edit) -> params.Schema:
    doc = json.loads((REPO / "backtest/config/params.json").read_text())
    doc.update(edit)
    return params.Schema(doc, RISK, ANALYST)


class SchemaTests(unittest.TestCase):
    def setUp(self):
        self.s = schema()

    def test_live_defaults_sit_on_the_grid_except_the_two_order_minimums(self):
        self.assertEqual(set(self.s.clipped()), {"sizing.minNewOrderInr", "sizing.minAdjustmentInr"})
        for p in self.s.params:
            self.assertTrue(p.on_grid(self.s.defaults()[p.key]), p.key)

    def test_defaults_apply_and_the_base_configs_are_untouched(self):
        before = json.dumps([RISK, ANALYST], sort_keys=True, default=str)
        risk, analyst = self.s.apply(self.s.defaults())
        self.assertEqual(risk["sizing"]["minNewOrderInr"], 10000)
        self.assertEqual(json.dumps([RISK, ANALYST], sort_keys=True, default=str), before)

    def test_wildcards_fan_out_and_list_indexes_write_into_lists(self):
        point = {"selector.topN.BULL": 3, "ladder.drawdown1": 0.08, "stops.clamp.MidCap.hi": 0.23}
        risk, analyst = self.s.apply(point)
        self.assertEqual({b: v["top_n"] for b, v in analyst["strategies"]["BULL"].items()}, {"LargeCap": 3, "MidCap": 3, "SmallCap": 3})
        self.assertEqual(risk["ladder"]["levels"][0]["drawdownPct"], 0.08)
        self.assertEqual(risk["stops"]["clampPct"]["MidCap"], [0.14, 0.23])

    def test_rejections(self):
        for point in ({"nope": 1}, {"selector.topN.BULL": 9}, {"stops.atrPeriod": 12}, {"stops.atrMultiplier": 3.25}):
            with self.assertRaises(params.InvalidPoint, msg=point):
                self.s.apply(point)
        with self.assertRaises(params.InvalidPoint):  # minAdjustment above minNewOrder
            self.s.apply({"sizing.minNewOrderInr": 2000, "sizing.minAdjustmentInr": 3000})
        with self.assertRaises(params.InvalidPoint):  # drawdown rungs out of order: caught by the app's own validator
            self.s.apply({"ladder.drawdown1": 0.14, "ladder.drawdown2": 0.11})

    def test_slow_sma_raises_the_minimum_rows_so_the_app_accepts_it(self):
        _, analyst = self.s.apply({"regime.smaSlow": 250})
        self.assertGreaterEqual(analyst["regime"]["minRows"], 260)

    def test_sampling_is_seeded_and_valid_and_neighbours_are_one_step_away(self):
        a, b = self.s.sample(random.Random(5)), self.s.sample(random.Random(5))
        self.assertEqual(a, b)
        self.s.apply(a)
        base = self.s.defaults()
        near = self.s.neighbours(base)
        self.assertGreater(len(near), 60)
        for q in near:
            diff = [k for k in base if q[k] != base[k]]
            self.assertEqual(len(diff), 1)
            vals = self.s.by_key[diff[0]].values()
            self.assertEqual(abs(vals.index(q[diff[0]]) - vals.index(base[diff[0]])), 1)

    def test_purge_warmup_and_common_start_follow_the_bounds(self):
        self.assertEqual(self.s.required_purge(), 250)  # the slowest allowed SMA
        self.assertEqual(self.s.warmup_rows(), 259)
        dates = [d.date().isoformat() for d in pd.bdate_range("2007-09-17", periods=1000)]
        self.assertEqual(self.s.common_start(dates), dates[259 + 5 * 6])
        with self.assertRaises(ValueError):
            self.s.common_start(dates[:200])

    def test_a_path_missing_from_the_live_config_is_caught_at_load(self):
        doc = json.loads((REPO / "backtest/config/params.json").read_text())
        doc["params"][0]["paths"] = ["strategies.NOPE.*.lookback"]
        with self.assertRaises(KeyError):
            params.Schema(doc, RISK, ANALYST)


class WindowTests(unittest.TestCase):
    def setUp(self):
        self.dates = [d.date().isoformat() for d in pd.bdate_range("2007-09-17", "2026-10-02")]
        self.s = schema()
        self.start = self.s.common_start(self.dates)
        self.w = walkforward.Windows(self.dates, self.start, BT["walkforward"], BT["window"]["holdoutYears"], self.s.required_purge())

    def pos(self, d):
        return self.dates.index(d)

    def test_holdout_is_the_last_two_years_and_the_purge_follows_the_look_back(self):
        self.assertEqual(self.w.purge, 250)
        self.assertTrue("2024-10-01" <= self.w.holdout_start <= "2024-10-04")
        self.assertEqual(self.pos(self.w.holdout_start) - self.pos(self.w.tuning_end) - 1, 250)

    def test_rolling_folds_are_purged_ordered_and_inside_the_tuning_region(self):
        folds = self.w.rolling()
        self.assertGreaterEqual(len(folds), 7)
        for f in folds:
            self.assertGreaterEqual(self.pos(f.test[0]) - self.pos(f.train[1]) - 1, 250)
            self.assertLessEqual(f.test[1], self.w.tuning_end)
            self.assertLess(f.train[1], f.test[0])
            self.assertTrue(4.9 < (pd.Timestamp(f.train[1]) - pd.Timestamp(f.train[0])).days / 365.25 < 5.1)
        for a, b in zip(folds, folds[1:]):
            self.assertLess(a.test[1], b.test[0])  # one-year tests that step one year do not overlap
            self.assertLess(a.train[0], b.train[0])

    def test_anchored_folds_keep_the_start_and_grow(self):
        folds = self.w.anchored()
        self.assertTrue(all(f.train[0] == self.start for f in folds))
        self.assertEqual([f.train[1] for f in folds], sorted(f.train[1] for f in folds))
        self.assertTrue(all(f.test[1] <= self.w.tuning_end for f in folds))
        self.assertLessEqual(len(folds), len(self.w.rolling()) + 1)

    def test_tuning_cannot_read_the_holdout(self):
        self.w.check_tuning(self.start, self.w.tuning_end)
        for end in (self.w.holdout_start, self.dates[-1], self.dates[self.pos(self.w.tuning_end) + 1]):
            with self.assertRaises(walkforward.HoldoutRead):
                self.w.check_tuning(self.start, end)

    def test_holdout_is_scored_once_for_one_parameter_set(self):
        marker = Path(self.id() + ".marker.json")
        self.addCleanup(marker.unlink, missing_ok=True)
        marker.unlink(missing_ok=True)
        self.assertEqual(self.w.holdout(marker, "A"), (self.w.holdout_start, self.dates[-1]))
        self.w.holdout(marker, "A")
        with self.assertRaises(walkforward.HoldoutRead):
            self.w.holdout(marker, "B")

    def test_no_room_for_tuning_is_refused(self):
        with self.assertRaises(ValueError):
            walkforward.Windows(self.dates[:1500], self.dates[1000], BT["walkforward"], 2, 250)


if __name__ == "__main__":
    unittest.main()
