"""Cash ledger, NAV, time-weighted index, jump check, cash_flows.csv rules."""

import unittest

import pandas as pd

from app.risk import nav
from tests.risk_helpers import Env


class FlowFileTests(Env):
    def test_exactly_one_opening_row_is_required(self):
        self.flows([("2026-01-02", "DEPOSIT", 1000)])
        with self.assertRaisesRegex(ValueError, "cash_flows.csv.*OPENING"):
            nav.read_flows(self.cfg, self.asof)
        self.flows([("2026-01-01", "OPENING", 1), ("2026-01-02", "OPENING", 2)])
        with self.assertRaisesRegex(ValueError, "exactly one OPENING"):
            nav.read_flows(self.cfg, self.asof)

    def test_unknown_types_and_future_dates_are_rejected(self):
        self.flows([("2026-01-01", "OPENING", 1), ("2026-01-02", "GIFT", 5)])
        with self.assertRaisesRegex(ValueError, "types must be one of"):
            nav.read_flows(self.cfg, self.asof)
        self.flows([("2026-01-01", "OPENING", 1), ("2026-12-31", "DEPOSIT", 5)])
        with self.assertRaisesRegex(ValueError, "not in the future"):
            nav.read_flows(self.cfg, self.asof)

    def test_non_numeric_amount_is_rejected(self):
        self.flows([("2026-01-01", "OPENING", "lots")])
        with self.assertRaises(ValueError):
            nav.read_flows(self.cfg, self.asof)


class CashTests(Env):
    def cash(self, flow_rows, fill_rows, asof=None):
        self.flows(flow_rows)
        self.fills(fill_rows)
        from app.analyst.ledger import read_fills
        return nav.ledger_cash(self.cfg, nav.read_flows(self.cfg, self.asof), read_fills(self.analyst / "ledger/fills.csv"), asof or self.asof)

    def test_opening_plus_deposits_dividends_minus_withdrawals(self):
        c = self.cash([("2026-01-01", "OPENING", 100000), ("2026-02-01", "DEPOSIT", 50000), ("2026-03-01", "DIVIDEND", 2000),
                       ("2026-04-01", "WITHDRAWAL", 30000), ("2026-05-01", "OTHER", -500)], [("2026-01-02", "X", "BUY", 1, 1.0)])
        self.assertAlmostEqual(c, 100000 + 50000 + 2000 - 30000 - 500 - self.buy_cost(1.0, 1))

    def buy_cost(self, price, qty):
        from app.analyst import costs
        n = price * qty
        return n + costs.buy_charges(self.cfg["costs"], n)

    def test_buys_cost_the_price_plus_charges_and_sells_return_it_minus_charges(self):
        from app.analyst import costs
        c = self.cash([("2026-01-01", "OPENING", 100000)], [("2026-02-02", "ABC", "BUY", 100, 200.0), ("2026-03-02", "ABC", "SELL", 100, 250.0)])
        buy, sell = 20000, 25000
        expected = 100000 - (buy + costs.buy_charges(self.cfg["costs"], buy)) + (sell - costs.sell_charges(self.cfg["costs"], sell))
        self.assertAlmostEqual(c, expected)

    def test_fills_before_the_opening_date_and_seed_rows_do_not_count(self):
        c = self.cash([("2026-02-01", "OPENING", 100000)], [("2026-01-15", "ABC", "BUY", 10, 100.0), ("2026-02-02", "ABC", "BUY", 10, 100.0, "SEED")])
        self.assertAlmostEqual(c, 100000)

    def test_fills_after_asof_do_not_count(self):
        c = self.cash([("2026-01-01", "OPENING", 100000)], [("2026-09-28", "ABC", "BUY", 10, 100.0)])
        self.assertAlmostEqual(c, 100000)

    def test_same_ticker_same_day_fills_pay_one_dp_charge(self):
        from app.analyst import costs
        c = self.cash([("2026-01-01", "OPENING", 0)], [("2026-02-02", "ABC", "SELL", 5, 100.0), ("2026-02-02", "ABC", "SELL", 5, 100.0)])
        self.assertAlmostEqual(c, 1000 - costs.sell_charges(self.cfg["costs"], 1000))


class NavRowTests(Env):
    def prev(self, nav_value=1000.0, twr=1.0):
        return pd.Series({"nav": nav_value, "twr_index": twr, "date": "2026-09-24"})

    def test_first_row_starts_the_index_at_one(self):
        row, jump = nav.nav_row("2026-09-25", 400.0, 600.0, 0.0, None)
        self.assertEqual((row["nav"], row["twr_index"], jump), (1000.0, 1.0, False))

    def test_deposit_and_withdrawal_do_not_move_the_index(self):
        row, _ = nav.nav_row("2026-09-25", 500.0, 1500.0, 1000.0, self.prev(1000.0))  # NAV doubled by a deposit alone
        self.assertAlmostEqual(row["twr_index"], 1.0)
        row, _ = nav.nav_row("2026-09-25", 300.0, 200.0, -500.0, self.prev(1000.0))  # NAV halved by a withdrawal alone
        self.assertAlmostEqual(row["twr_index"], 1.0)

    def test_market_gain_moves_the_index(self):
        row, _ = nav.nav_row("2026-09-25", 550.0, 550.0, 0.0, self.prev(1000.0, 1.0))
        self.assertAlmostEqual(row["twr_index"], 1.1)

    def test_nav_jump_warns_only_without_a_flow(self):
        self.assertTrue(nav.nav_row("d", 800.0, 400.0, 0.0, self.prev(1000.0))[1])  # +20%
        self.assertFalse(nav.nav_row("d", 800.0, 400.0, 200.0, self.prev(1000.0))[1])  # explained by a flow
        self.assertFalse(nav.nav_row("d", 700.0, 400.0, 0.0, self.prev(1000.0))[1])  # +10%

    def test_external_flow_sums_rows_after_the_previous_run(self):
        self.flows([("2026-01-01", "OPENING", 100), ("2026-09-23", "DEPOSIT", 50), ("2026-09-24", "WITHDRAWAL", 20),
                    ("2026-09-25", "DIVIDEND", 7), ("2026-09-25", "OTHER", -3), ("2026-09-22", "DEPOSIT", 999)])
        f = nav.read_flows(self.cfg, self.asof)
        self.assertEqual(nav.external_flow(f, "2026-09-22", "2026-09-25"), 50 - 20 - 3)  # opening cash and dividends are not flows

    def test_nav_rows_are_replaced_by_date(self):
        path = self.risk / "nav" / "nav_actual.csv"
        row = {"date": "2026-09-24", "positions_value": 1.0, "cash": 2.0, "nav": 3.0, "flow": 0.0, "twr_index": 1.0, "bench_close": 1.0, "active_regime": "BULL", "rung": 0}
        nav.upsert_nav(path, row)
        nav.upsert_nav(path, {**row, "date": "2026-09-25"})
        nav.upsert_nav(path, {**row, "nav": 9.0})
        df = nav.read_nav(path)
        self.assertEqual(list(df.date), ["2026-09-24", "2026-09-25"])
        self.assertEqual(df.nav.iloc[0], 9.0)


if __name__ == "__main__":
    unittest.main()
