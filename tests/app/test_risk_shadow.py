"""Shadow portfolio: start copy, fills at the next Open with slippage and charges, shortfall, missing Open, independence, idempotence."""

import json
import unittest

import pandas as pd

from app.analyst import costs
from app.analyst.ledger import read_fills
from app.risk import shadow
from tests.app.risk_helpers import FRIDAY, THURSDAY, Env


def signal(asof, execution, actions):
    return {"asOf": asof, "executionDate": execution, "actions": actions}


def act(ticker, side, qty, bucket="SmallCap"):
    return {"ticker": ticker, "bucket": bucket, "side": side, "qty": qty}


class ApplyEnv(Env):
    """A shadow with one seeded position and a hand-written signal file executing on Friday."""

    def setUp(self):
        super().setUp()
        self.buckets(SmallCap=["XYZ", "NEW"])
        opens = [250.0] * (len(self.days) - 1) + [240.0]  # Friday opens at 240
        self.price("XYZ", opens=opens)
        self.price("NEW", opens=[100.0] * (len(self.days) - 1) + [100.0], base=100.0)

    def seed(self, cash=100000.0, book=(("XYZ", 40, 200.0, "2026-01-05"),)):
        actual = self.portfolio(book=pd.DataFrame(list(book), columns=["ticker", "qty", "avg_price", "entry_date"]).assign(entry_source="FILLS"), cash=cash)
        shadow.seed(self.cfg, actual, THURSDAY)

    def signals_file(self, asof, execution, actions):
        folder = self.risk / "shadow" / "signals"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"signals_{asof}.json").write_text(json.dumps(signal(asof, execution, actions)))

    def apply(self, asof=FRIDAY):
        ctx = self.context(asof=asof)
        return shadow.apply(ctx), ctx

    def fills(self):
        return read_fills(self.risk / "shadow" / "fills.csv")


class SeedTests(ApplyEnv):
    def test_start_copies_book_with_entry_dates_and_cash(self):
        self.seed(cash=123456.0, book=(("XYZ", 40, 200.0, "2026-01-05"), ("NEW", 10, 90.0, "UNKNOWN")))
        f = self.fills()
        self.assertEqual(list(f.kind), ["SEED", "UNKNOWN"])
        self.assertEqual(list(f.trade_date), ["2026-01-05", THURSDAY])
        pf, _ = self.apply(THURSDAY)
        self.assertEqual(pf.cash, 123456.0)
        book = pf.book.set_index("ticker")
        self.assertEqual((book.qty["XYZ"], book.avg_price["XYZ"], book.entry_date["XYZ"], book.entry_date["NEW"]), (40, 200.0, "2026-01-05", "UNKNOWN"))

    def test_an_empty_book_starts_an_empty_shadow(self):
        self.seed(book=())
        pf, _ = self.apply(THURSDAY)
        self.assertEqual((len(pf.book), pf.cash), (0, 100000.0))


class FillTests(ApplyEnv):
    def test_buy_fills_at_next_open_plus_slippage_and_charges(self):
        self.seed(book=())
        self.signals_file(THURSDAY, FRIDAY, [act("XYZ", "BUY", 100)])
        pf, _ = self.apply()
        price = 240.0 * (1 + 50 / 10000)  # SmallCap 50 bps
        n = 100 * price
        f = self.fills()
        self.assertEqual((f.side[0], int(f.qty[0]), f.trade_date[0], f.kind[0], round(float(f.price[0]), 4)), ("BUY", 100, FRIDAY, "FILL", round(price, 4)))
        self.assertAlmostEqual(pf.cash, 100000 - n - costs.buy_charges(self.cfg["costs"], n), places=1)
        self.assertEqual((pf.book.qty[0], pf.book.entry_date[0]), (100, FRIDAY))

    def test_sell_fills_at_open_minus_slippage_less_charges(self):
        self.seed()
        self.signals_file(THURSDAY, FRIDAY, [act("XYZ", "SELL", 40)])
        pf, _ = self.apply()
        price = 240.0 * (1 - 50 / 10000)
        n = 40 * price
        self.assertAlmostEqual(pf.cash, 100000 + n - costs.sell_charges(self.cfg["costs"], n), places=1)
        self.assertEqual(len(pf.book), 0)

    def test_sell_is_capped_at_the_quantity_held(self):
        self.seed()
        self.signals_file(THURSDAY, FRIDAY, [act("XYZ", "SELL", 500)])
        pf, _ = self.apply()
        self.assertEqual(int(self.fills().qty.iloc[-1]), 40)
        self.assertEqual(len(pf.book), 0)

    def test_buy_beyond_cash_is_reduced_to_affordable_whole_shares_with_a_warning(self):
        self.seed(cash=5000.0, book=())
        self.signals_file(THURSDAY, FRIDAY, [act("XYZ", "BUY", 100)])
        pf, ctx = self.apply()
        qty = int(self.fills().qty.iloc[-1])
        price = 240.0 * 1.005
        self.assertTrue(0 < qty < 100 and qty * price + costs.buy_charges(self.cfg["costs"], qty * price) <= 5000.0)
        self.assertGreaterEqual(pf.cash, 0)
        self.assertIn("SHADOW_SHORTFALL:XYZ", ctx.warnings)

    def test_buy_with_no_affordable_share_is_skipped(self):
        self.seed(cash=100.0, book=())
        self.signals_file(THURSDAY, FRIDAY, [act("XYZ", "BUY", 100)])
        pf, ctx = self.apply()
        self.assertEqual((len(self.fills()), pf.cash), (0, 100.0))
        self.assertIn("SHADOW_SHORTFALL:XYZ", ctx.warnings)

    def test_signals_execute_only_on_their_execution_day(self):
        self.seed()
        self.signals_file(FRIDAY, "2026-09-28", [act("XYZ", "SELL", 40)])  # executes next Monday
        pf, _ = self.apply()
        self.assertEqual(len(pf.book), 1)

    def test_missing_open_leaves_the_action_to_be_retried_the_next_trading_day(self):
        self.seed()
        self.price("XYZ", opens=[250.0] * (len(self.days) - 2) + [0.0, 240.0])  # Thursday has no Open
        self.signals_file("2026-09-23", THURSDAY, [act("XYZ", "SELL", 40)])
        pf, ctx = self.apply(THURSDAY)
        self.assertEqual(len(pf.book), 1)
        self.assertIn("SHADOW_NO_OPEN:XYZ", ctx.warnings)
        pf, ctx = self.apply(FRIDAY)  # Friday has an Open: the same signal fills now
        self.assertEqual((len(pf.book), self.fills().trade_date.iloc[-1]), (0, FRIDAY))
        self.assertEqual(ctx.warnings, [])

    def test_a_repeating_stop_signal_fills_once(self):
        self.seed()
        self.signals_file("2026-09-22", "2026-09-23", [act("XYZ", "SELL", 40)])
        self.signals_file("2026-09-23", THURSDAY, [act("XYZ", "SELL", 40)])
        self.price("XYZ", opens=[250.0] * (len(self.days) - 3) + [0.0, 250.0, 240.0])  # Wed (09-23) has no Open
        self.apply("2026-09-23")
        pf, _ = self.apply(THURSDAY)
        self.assertEqual(len(self.fills()[self.fills().kind == "FILL"]), 1)
        self.assertEqual(len(pf.book), 0)

    def test_rerunning_the_same_day_is_idempotent(self):
        self.seed()
        self.signals_file(THURSDAY, FRIDAY, [act("XYZ", "SELL", 20), act("NEW", "BUY", 10, "SmallCap")])
        first, _ = self.apply()
        fills, cash = self.fills().copy(), first.cash
        second, _ = self.apply()
        self.assertEqual((len(self.fills()), second.cash), (len(fills), cash))
        pd.testing.assert_frame_equal(first.book.reset_index(drop=True), second.book.reset_index(drop=True))
        self.assertEqual(len(pd.read_csv(self.risk / "shadow/cash.csv")), 2)  # start row + Friday

    def test_old_signal_files_are_not_replayed(self):
        self.seed()
        self.signals_file("2026-09-01", "2026-09-02", [act("XYZ", "SELL", 40)])  # far outside the retry window
        pf, _ = self.apply()
        self.assertEqual(len(pf.book), 1)


class ShadowRunTests(Env):
    """Through Run: the shadow follows every signal one session later, with its own state."""

    def setUp(self):
        super().setUp()
        self.now = self.set_day(THURSDAY)
        self.buckets(SmallCap=["XYZ"])
        self.surveillance(day=THURSDAY)
        n = len(self.days_to(THURSDAY))
        self.price("XYZ", closes=[300.0] * (n - 1) + [245.0], days=self.days_to(THURSDAY))
        self.book([("XYZ", 40, 280.0, "2026-01-05")])

    def friday(self):
        self.now = self.set_day(FRIDAY)
        self.surveillance()
        self.targets({})
        self.price("XYZ", closes=[300.0] * 398 + [245.0, 241.0], opens=[300.0] * 399 + [240.0])
        self.assertEqual(self.run_risk(self.now).status(), "ok")

    def test_first_run_starts_the_shadow_from_the_actual_book_and_records_the_date(self):
        self.assertEqual(self.run_risk(self.now).status(), "ok")
        state = json.loads((self.risk / "state/ladder_state.json").read_text())
        self.assertEqual(state["shadowStartDate"], THURSDAY)
        sig = json.loads((self.risk / "shadow/signals/signals_2026-09-24.json").read_text())
        self.assertEqual([(a["ticker"], a["reason"]) for a in sig["actions"]], [("XYZ", "STOP")])  # the shadow stops out too
        row = pd.read_csv(self.risk / "nav/nav_shadow.csv").iloc[0]
        self.assertEqual((row.nav, row.twr_index), (pd.read_csv(self.risk / "nav/nav_actual.csv").nav[0], 1.0))

    def test_the_stop_signal_fills_at_the_next_open(self):
        self.run_risk(self.now)
        self.friday()
        fill = self.fills_df().iloc[-1]
        self.assertEqual((fill.side, fill.trade_date, round(float(fill.price), 4)), ("SELL", FRIDAY, round(240 * 0.995, 4)))
        sig = json.loads((self.risk / "shadow/signals/signals_2026-09-25.json").read_text())
        self.assertEqual(sig["actions"], [])  # the shadow no longer holds it
        self.assertEqual(sig["positions"], [])

    def test_shadow_state_is_independent_of_the_actual_book(self):
        self.run_risk(self.now)
        self.book([("OTHER", 5, 100.0, "2026-09-10")])  # the actual account diverges
        self.price("OTHER", base=100.0)
        self.buckets(SmallCap=["XYZ", "OTHER"])
        self.friday()
        shadow_sig = json.loads((self.risk / "shadow/signals/signals_2026-09-25.json").read_text())
        self.assertNotIn("OTHER", [p["ticker"] for p in shadow_sig["positions"]])  # never reads the actual book after the start
        self.assertEqual(len(pd.read_csv(self.risk / "shadow/state/cooldown.csv")), 1)  # its own cooldown
        self.assertEqual(pd.read_csv(self.risk / "shadow/state/cooldown.csv").ticker[0], "XYZ")

    def test_shadow_has_its_own_ladder(self):
        self.run_risk(self.now)
        self.assertTrue((self.risk / "shadow/state/ladder_state.json").exists())
        self.assertNotEqual(self.risk / "shadow/state/ladder_state.json", self.risk / "state/ladder_state.json")

    def test_shadow_can_be_disabled(self):
        self.cfg["shadow"]["enabled"] = False
        self.assertEqual(self.run_risk(self.now).status(), "ok")
        self.assertFalse((self.risk / "shadow").exists())
        self.assertFalse((self.risk / "nav/nav_shadow.csv").exists())

    def test_shadow_warnings_go_to_the_digest_not_the_actual_signal_file(self):
        self.run_risk(self.now)
        self.now = self.set_day(FRIDAY)
        self.surveillance()
        self.targets({})
        self.price("XYZ", closes=[300.0] * 398 + [245.0, 241.0], opens=[300.0] * 399 + [0.0])  # no Open on Friday
        report = self.run_risk(self.now)
        self.assertTrue(any("SHADOW_NO_OPEN:XYZ" in line for line in report.lines))
        self.assertNotIn("SHADOW_NO_OPEN:XYZ", self.signals()["warnings"])

    def fills_df(self):
        return read_fills(self.risk / "shadow/fills.csv")


if __name__ == "__main__":
    unittest.main()


class CarryOverDays(ApplyEnv):
    def test_h9_default_is_seven_days(self):
        self.assertEqual(shadow.carry_over_days(self.cfg), 7)
        self.assertEqual(shadow.carry_over_days({}), 7)

    def test_h9_a_signal_older_than_the_carry_over_is_dropped(self):
        for days, filled in ((7, True), (3, False)):
            self.seed()
            self.cfg["shadow"]["carryOverDays"] = days
            self.signals_file("2026-09-18", FRIDAY, [act("XYZ", "SELL", 40)])  # 7 calendar days old on Friday 09-25
            pf, _ = self.apply()
            self.assertEqual(len(pf.book) == 0, filled, days)
