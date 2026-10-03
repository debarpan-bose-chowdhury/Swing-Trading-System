import unittest

import numpy as np
import pandas as pd

from backtest import overfit


def noise(rng, t, n, sd=0.01):
    return pd.DataFrame(rng.normal(0, sd, (t, n)), columns=[f"t{i}" for i in range(n)])


class SharpeTests(unittest.TestCase):
    def test_pure_noise_best_of_many_is_not_deflated_significant_but_a_real_edge_is(self):
        rng = np.random.default_rng(1)
        m = noise(rng, 2500, 100)
        sr = np.array([overfit.sharpe(m[c].to_numpy()) for c in m])
        best = m.iloc[:, int(sr.argmax())].to_numpy()
        self.assertLess(overfit.deflated_sharpe(best, sr), 0.95)
        edge = rng.normal(0.0012, 0.01, 2500)
        sr2 = np.append(sr, overfit.sharpe(edge))
        self.assertGreater(overfit.deflated_sharpe(edge, sr2), 0.95)

    def test_more_trials_deflate_more(self):
        rng = np.random.default_rng(2)
        r = rng.normal(0.0006, 0.01, 2000)
        sr = np.array([overfit.sharpe(rng.normal(0, 0.01, 2000)) for _ in range(30)])
        self.assertGreater(overfit.deflated_sharpe(r, sr, 5), overfit.deflated_sharpe(r, sr, 5000))

    def test_needs_trials_and_data(self):
        with self.assertRaises(ValueError):
            overfit.deflated_sharpe(np.zeros(100), np.array([0.1]))


class PboTests(unittest.TestCase):
    def test_noise_trials_overfit_about_half_the_time(self):
        pbo = overfit.pbo_cscv(noise(np.random.default_rng(3), 1600, 40))
        self.assertEqual(pbo["splits"], 12870)
        self.assertTrue(0.3 < pbo["pbo"] < 0.7, pbo)

    def test_a_real_edge_among_noise_does_not(self):
        m = noise(np.random.default_rng(4), 1600, 40)
        m["edge"] = np.random.default_rng(5).normal(0.002, 0.01, 1600)
        self.assertLess(overfit.pbo_cscv(m)["pbo"], 0.05)

    def test_input_checks(self):
        with self.assertRaises(ValueError):
            overfit.pbo_cscv(noise(np.random.default_rng(0), 100, 1))
        with self.assertRaises(ValueError):
            overfit.pbo_cscv(noise(np.random.default_rng(0), 100, 5), blocks=7)


class RetentionAndNeighbourhoodTests(unittest.TestCase):
    def test_retention(self):
        self.assertAlmostEqual(overfit.retention([0.2, 0.3], [0.15, 0.15]), 0.6)
        self.assertIsNone(overfit.retention([-0.1, 0.05], [0.1, 0.1]))

    def test_neighbourhood_plateau_vs_spike(self):
        c = {"postTaxCagr": 0.20, "maxDrawdown": -0.20, "ulcerIndex": 6.0}
        plateau = [{"postTaxCagr": 0.19, "maxDrawdown": -0.21, "ulcerIndex": 6.2}, {"postTaxCagr": 0.25, "maxDrawdown": -0.15, "ulcerIndex": 5.0}]
        spike = [{"postTaxCagr": 0.05, "maxDrawdown": -0.40, "ulcerIndex": 12.0}] * 2
        self.assertTrue(overfit.neighbourhood(c, plateau, 0.2, 0.8)["passed"])
        self.assertFalse(overfit.neighbourhood(c, spike, 0.2, 0.8)["passed"])
        self.assertFalse(overfit.neighbourhood(c, [], 0.2, 0.8)["passed"])
        mixed = plateau + spike
        self.assertEqual(overfit.neighbourhood(c, mixed, 0.2, 0.8)["share"], 0.5)

    def test_pareto_front(self):
        df = pd.DataFrame({"postTaxCagr": [0.2, 0.15, 0.2, 0.1], "maxDrawdown": [-0.2, -0.1, -0.3, -0.4], "ulcerIndex": [5.0, 3.0, 6.0, 9.0]})
        self.assertEqual(list(overfit.pareto_front(df)), [True, True, False, False])

    def test_gate_needs_all_four(self):
        lim = {"pboMax": 0.2, "dsrMin": 0.95, "oosIsMin": 0.6, "neighbourhoodShare": 0.8, "neighbourhoodTolerance": 0.2}
        ok = overfit.gate(lim, {"pbo": 0.1}, 0.97, 0.9, 0.8, {"passed": True})
        self.assertTrue(ok["passed"])
        for args in (({"pbo": 0.3}, 0.97, 0.9, 0.8, {"passed": True}), ({"pbo": 0.1}, 0.9, 0.9, 0.8, {"passed": True}),
                     ({"pbo": 0.1}, 0.97, 0.5, 0.8, {"passed": True}), ({"pbo": 0.1}, 0.97, None, 0.8, {"passed": True}),
                     ({"pbo": 0.1}, 0.97, 0.9, 0.8, {"passed": False})):
            self.assertFalse(overfit.gate(lim, *args)["passed"])


if __name__ == "__main__":
    unittest.main()
