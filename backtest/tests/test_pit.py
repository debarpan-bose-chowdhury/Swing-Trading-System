import unittest

import numpy as np
import pandas as pd

from backtest import pit
from backtest.tests.helpers import TreeCase, bars, weekdays


class PitTests(TreeCase):
    def setUp(self):
        super().setUp()
        self.days = weekdays("2024-11-01", 80)  # crosses the archive cutoff 2025-01-01
        self.bucket("LargeCap", ["AAA", "BBB"])
        self.bucket("MidCap", ["CCC"])
        self.put("AAA", bars("AAA", self.days, np.linspace(100, 180, 80)))
        self.put("BBB", bars("BBB", self.days[10:], 50.0))
        self.put("^NSEI", bars("^NSEI", self.days, 10000.0), index=True)

    def load(self):
        return pit.PitData.load("app/data", ["LargeCap", "MidCap", "SmallCap"])

    def test_both_tiers_merge_in_order_and_symbols_without_history_are_reported(self):
        d = self.load()
        self.assertEqual(list(d.series["AAA"].Date), self.days)  # archive + fresh, no duplicates
        self.assertEqual(d.missing(), ["CCC"])
        self.assertEqual(d.buckets["SmallCap"], [])

    def test_store_view_hides_the_future(self):
        d = self.load()
        store = pit.PitStore(d)
        store.asof = self.days[30]
        df = store.read_fresh("AAA")
        self.assertEqual(df.Date.iloc[-1], self.days[30])
        self.assertEqual(len(df), 31)
        self.assertTrue(store.read_archive("AAA").empty and store.read_fresh("ZZZ").empty)
        store.asof = "2020-01-01"
        self.assertTrue(store.read_fresh("AAA").empty)

    def test_store_requires_asof(self):
        with self.assertRaises(RuntimeError):
            pit.PitStore(self.load()).read_fresh("AAA")

    def test_works_with_the_apps_history_helper(self):
        from app.risk.common import history
        store = pit.PitStore(self.load())
        store.asof = self.days[40]
        df = history(store, "AAA", self.days[35], self.days[40])
        self.assertEqual(df.Date.iloc[-1], self.days[40])

    def test_panels_align_and_value_is_close_times_volume(self):
        adj, value = self.load().panels()
        self.assertEqual(adj.shape, (80, 2))
        self.assertTrue(np.isnan(adj.BBB.iloc[0]) and adj.BBB.iloc[10] == 50.0)
        self.assertEqual(value.BBB.iloc[10], 50.0 * 1000)

    def test_bench_close(self):
        d = self.load()
        self.assertEqual(d.bench_close(self.days[5]), 10000.0)
        self.assertIsNone(d.bench_close("2000-01-01"))

    def test_hash_is_stable_and_sensitive(self):
        a, b = self.load().data_hash(), self.load().data_hash()
        self.assertEqual(a, b)
        d = self.load()
        d.series["AAA"].loc[3, "Close"] += 0.01
        self.assertNotEqual(a, d.data_hash())


if __name__ == "__main__":
    unittest.main()
