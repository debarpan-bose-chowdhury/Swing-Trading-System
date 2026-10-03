import unittest

import pandas as pd

from backtest import bench, walkforward, workers
from backtest.tests.test_params_walkforward import BT, schema


class BenchTests(unittest.TestCase):
    def test_gate_workload_counts_every_simulation_it_needs(self):
        s = schema()
        dates = [d.date().isoformat() for d in pd.bdate_range("2007-09-17", "2026-10-02")]
        wf = walkforward.Windows(dates, s.common_start(dates), BT["walkforward"], 2, s.required_purge())
        g = bench.gate_simulations(s, wf, 30)
        self.assertEqual(g["runs"], 30 + 2 * g["folds"] + g["neighbours"])
        self.assertGreater(g["neighbours"], 60)
        self.assertGreater(g["years"], 30 * 14)  # each tried point runs the whole tuning region
        self.assertAlmostEqual(g["spanYears"], 14.9, delta=0.5)

    def test_missing_data_is_exit_3(self):
        import os, tempfile
        previous = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                from pathlib import Path
                import shutil
                shutil.copytree(Path(__file__).parents[2] / "backtest/config", "backtest/config")
                shutil.copytree(Path(__file__).parents[2] / "app/config", "app/config")
                self.assertEqual(bench.main([]), 3)
            finally:
                os.chdir(previous)


if __name__ == "__main__":
    unittest.main()


class SetParsing(unittest.TestCase):
    def test_set_pairs(self):
        self.assertEqual(bench.parse_set(["sizing.minNewOrderInr=3000", "stops.atrMultiplier=3.5"]), {"sizing.minNewOrderInr": 3000, "stops.atrMultiplier": 3.5})
        with self.assertRaises(ValueError):
            bench.parse_set(["nokey"])


class Threads(unittest.TestCase):
    def test_limit_threads_sets_every_native_pool_to_one(self):
        import os
        saved = {k: os.environ.get(k) for k in workers.THREAD_ENV}
        try:
            workers.limit_threads()
            self.assertTrue(all(os.environ[k] == "1" for k in workers.THREAD_ENV))
        finally:
            for k, v in saved.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
