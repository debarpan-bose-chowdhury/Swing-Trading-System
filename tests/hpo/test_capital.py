"""The capital calculator against the sizer's own arithmetic: the formula, and the real engine on the synthetic world (no entries below the threshold, entries above it)."""

import unittest

from backtest import api, world
from hpo import capital
from tests.hpo.test_objective import EngineCase
from tests.hpo.test_space import load_space


class FormulaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _, cls.risk, cls.analyst, _ = load_space()

    def rows(self, risk=None, analyst=None, price=2000.0):
        return capital.required(risk or self.risk, analyst or self.analyst, price)

    def test_each_limit_is_the_one_that_binds_where_the_sizer_says(self):
        by = {(r["regime"], r["bucket"]): r for r in self.rows()["rows"]}
        self.assertEqual((by["BULL", "LargeCap"]["limitedBy"], by["BULL", "LargeCap"]["fraction"]), ("NAME_CAP", 0.10))
        self.assertEqual(by["BEAR", "LargeCap"]["limitedBy"], "BUCKET_BUDGET")
        self.assertAlmostEqual(by["BEAR", "LargeCap"]["fraction"], 0.5 / 8)
        self.assertAlmostEqual(by["BULL", "LargeCap"]["needed"], (25000 + 2000) / 0.10)

    def test_the_risk_target_binds_when_it_is_the_smallest(self):
        risk = {**self.risk, "sizing": {**self.risk["sizing"], "riskPerPositionPct": 0.004}}
        row = next(r for r in self.rows(risk)["rows"] if (r["regime"], r["bucket"]) == ("BULL", "LargeCap"))
        self.assertEqual(row["limitedBy"], "RISK_TARGET")
        self.assertAlmostEqual(row["fraction"], 0.004 / 0.10)

    def test_the_account_that_clears_every_regime_is_the_bear_regime_with_the_biggest_bucket(self):
        r = self.rows()
        self.assertEqual(r["everyRegime"], r["bestPerRegime"]["BEAR"]["needed"])
        self.assertEqual(r["bestPerRegime"]["BEAR"]["bucket"], "LargeCap")
        self.assertLess(r["namecapOnly"], r["anyRegime"])  # the one-line check understates it
        self.assertEqual(capital.round_up(432000), 450000)

    def test_a_lower_minimum_order_lowers_the_account_and_a_bigger_share_price_raises_it(self):
        risk = {**self.risk, "sizing": {**self.risk["sizing"], "minNewOrderInr": 5000}}
        self.assertLess(self.rows(risk)["everyRegime"], self.rows()["everyRegime"])
        self.assertGreater(self.rows(price=8000.0)["everyRegime"], self.rows()["everyRegime"])


class ShippedCapitalTests(unittest.TestCase):
    def test_the_shipped_capital_clears_the_shipped_sizing_in_every_regime(self):
        """Reads app/config and backtest.json as shipped (not the pinned test copies): retuning the sizing must not leave the capital too small to trade."""
        bt, risk, analyst = api.base_configs()
        need = capital.required(risk, analyst, 2000.0)["everyRegime"]
        self.assertGreaterEqual(bt["capital"]["inr"], need, f"raise backtest.json capital.inr to at least Rs {capital.round_up(need):,} (hpo.cli capital)")
        self.assertGreaterEqual(analyst["capital"]["floatingCapitalInr"], need)
        self.assertGreaterEqual(bt["capital"]["inr"], 100000)


class EngineGateTests(EngineCase):
    """The live sizing minimum (25,000), not the test's 3,000: entries appear only once the account clears the arithmetic."""

    def setUp(self):
        super().setUp()
        self.bt["overrides"]["risk"] = {}
        self.w = world.World.build(self.bt)

    def fills(self, nav: float) -> int:
        ev = api.evaluate_config(self.w, self.w.risk, self.w.analyst, self.days[300], self.days[400], capital=nav)
        return len(ev.fills)

    def test_no_entry_below_the_threshold_and_entries_above_it(self):
        price = max(float(df.Close.max()) for df in self.w.data.series.values())
        need = capital.required(self.w.risk, self.w.analyst, price)["anyRegime"]
        pure = self.w.risk["sizing"]["minNewOrderInr"] / max(self.w.risk["sizing"]["nameCapPct"].values())
        self.assertEqual(self.fills(0.9 * pure), 0)  # under minimum order / largest name cap: impossible for any price
        self.assertGreater(self.fills(1.3 * need), 0)


if __name__ == "__main__":
    unittest.main()
