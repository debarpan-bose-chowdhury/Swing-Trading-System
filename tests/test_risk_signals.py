"""Signals end to end: resizing, DROP reasons, one SELL per ticker, liquidity, and a Thursday-to-Monday lifecycle."""

import json
import unittest

import pandas as pd

from app.risk import common
from tests.risk_helpers import FRIDAY, NOW, THURSDAY, Env

MONDAY = "2026-09-28"


class SignalEnv(Env):
    def setUp(self):
        super().setUp()
        self.surveillance()
        self.buckets(SmallCap=["XYZ", "ABC"], MidCap=["MID"], LargeCap=[])

    def go(self, now=NOW, **kw):
        report = self.run_risk(now, **kw)
        self.assertEqual(report.status(), "ok", report.error)
        return self.signals()


class ResizeTests(SignalEnv):
    COMP = {"LargeCap": 0, "MidCap": 1, "SmallCap": 0}

    def hold(self, qty):
        self.price("MID")
        self.flows([("2026-01-01", "OPENING", 700000 - qty * 250.0)])
        self.book([("MID", qty, 250.0, "2026-01-05")])
        self.targets({"MidCap": ["MID"]}, comp=self.COMP)

    def test_overweight_keep_outside_the_band_is_trimmed(self):
        self.hold(297)
        a, = self.go()["actions"]
        self.assertEqual((a["side"], a["kind"], a["qty"], a["reason"], a["priority"]), ("SELL", "TRIM", 73, "REBALANCE_TRIM", 5))

    def test_overweight_keep_inside_the_band_is_left_alone(self):
        self.hold(291)
        self.assertEqual(self.go()["actions"], [])

    def test_underweight_keep_is_topped_up(self):
        self.hold(80)
        a, = self.go()["actions"]
        self.assertEqual((a["side"], a["kind"], a["qty"], a["reason"], a["priority"]), ("BUY", "TOPUP", 144, "TOPUP", 6))

    def test_a_stopped_name_that_is_also_selected_is_only_sold(self):
        self.price("MID", closes=[300.0] * 399 + [200.0])
        self.flows([("2026-01-01", "OPENING", 600000)])
        self.book([("MID", 400, 250.0, "2026-01-05")])
        self.targets({"MidCap": ["MID"]}, comp=self.COMP)
        acts = self.go()["actions"]
        self.assertEqual([(a["ticker"], a["side"], a["reason"]) for a in acts], [("MID", "SELL", "STOP")])  # no BUY for a name with a SELL


class DropTests(SignalEnv):
    def setUp(self):
        super().setUp()
        self.price("XYZ")
        self.price("ABC")
        self.book([("XYZ", 40, 250.0, "2026-06-01")])

    def reason(self, **kw):
        self.targets({"SmallCap": ["ABC"]}, **kw)
        return {a["ticker"]: a for a in self.go(force=True)["actions"]}["XYZ"]["reason"]

    def test_drop_reasons(self):
        self.assertEqual(self.reason(), "DROP_NOT_SELECTED")
        self.assertEqual(self.reason(comp={"LargeCap": 0, "MidCap": 1, "SmallCap": 0}), "DROP_NO_ALLOCATION")  # weight 0
        self.assertEqual(self.reason(top_n=0), "DROP_NO_ALLOCATION")
        self.assertEqual(self.reason(regime=("BULL", "Unknown")), "DROP_UNKNOWN_REGIME")

    def test_drop_is_a_full_exit_with_priority_4(self):
        self.targets({"SmallCap": ["ABC"]})
        a = {x["ticker"]: x for x in self.go()["actions"]}["XYZ"]
        self.assertEqual((a["kind"], a["qty"], a["priority"], a["detail"]["dropReason"]), ("EXIT", 40, 4, "NOT_SELECTED"))

    def test_priority_orders_sells_before_buys(self):
        self.price("MID", closes=[300.0] * 399 + [240.0])
        self.book([("XYZ", 40, 250.0, "2026-06-01"), ("MID", 40, 250.0, "2026-06-01")])
        self.surveillance(gsm=["XYZ"])
        self.targets({"SmallCap": ["ABC"]})
        acts = self.go()["actions"]
        self.assertEqual([(a["ticker"], a["reason"], a["priority"]) for a in acts], [("MID", "STOP", 1), ("XYZ", "SURVEILLANCE", 2), ("ABC", "ENTRY", 6)])
        self.assertEqual(len({a["ticker"] for a in acts if a["side"] == "SELL"}), 2)  # one SELL per ticker


class LiquidityTests(SignalEnv):
    def test_a_sell_larger_than_the_adv_cap_warns_but_is_never_capped(self):
        self.price("XYZ", closes=[300.0] * 399 + [200.0], volume=1000)  # ADV ~Rs 3 lakh: cap 0.5% = Rs 1,500
        self.book([("XYZ", 40, 250.0, "2026-06-01")])
        self.targets({})
        sig = self.go()
        a, = sig["actions"]
        self.assertEqual((a["reason"], a["qty"]), ("STOP", 40))
        self.assertIn("LIQUIDITY:XYZ", sig["warnings"])

    def test_adv_cap_limits_a_buy(self):
        self.price("ABC", volume=28000)  # ADV Rs 7 million: cap 0.5% = 35,000 -> 140 shares
        self.targets({"SmallCap": ["ABC"]})
        a, = self.go()["actions"]
        self.assertEqual((a["qty"], a["detail"]["limitedBy"]), (140, "ADV_CAP"))


class LifecycleTests(SignalEnv):
    """Thursday (daily), Friday (weekly entry), Monday (the fill shows up in the book)."""

    def setUp(self):
        super().setUp()
        self.extra = self.days + [MONDAY]
        for t in ("ABC", "XYZ"):
            self.price(t, days=self.extra)
        self.index(days=self.extra)

    def day(self, asof):
        now = self.set_day(asof)
        self.surveillance(day=asof)
        return now

    def finish(self, report, now):
        common.write_status(self.cfg, report, now)
        return report

    def test_a_week_of_runs(self):
        thursday = self.day(THURSDAY)
        self.finish(self.run_risk(thursday), thursday)
        friday = self.day(FRIDAY)
        self.targets({"SmallCap": ["ABC"]})
        self.finish(self.run_risk(friday), friday)
        entry, = self.signals(FRIDAY)["actions"]
        self.assertEqual((entry["ticker"], entry["kind"], entry["qty"]), ("ABC", "ENTRY", 168))
        # Monday: the Ledger shows the buy at the open (250.2); the shadow filled the same signal at 250 x 1.005
        self.regimes([(FRIDAY, "BULL", "BULL")])
        monday = self.day(MONDAY)
        self.book([("ABC", 168, 250.2, MONDAY)])
        self.fills([(MONDAY, "ABC", "BUY", 168, 250.2)])
        report = self.finish(self.run_risk(monday), monday)
        self.assertEqual(report.status(), "ok", report.error)
        sig = self.signals(MONDAY)
        self.assertEqual(sig["weekly"]["included"], False)
        self.assertEqual(sig["actions"], [])
        p, = sig["positions"]
        self.assertEqual((p["ticker"], p["qty"], p["trackStart"]), ("ABC", 168, MONDAY))
        navs = pd.read_csv(self.risk / "nav/nav_actual.csv")
        self.assertEqual(list(navs.date), [THURSDAY, FRIDAY, MONDAY])
        self.assertLess(navs.nav.iloc[-1], 700000)  # charges and slippage are the only change: the price did not move
        shadow = json.loads((self.risk / "shadow/signals/signals_2026-09-28.json").read_text())
        self.assertEqual([(x["ticker"], x["qty"]) for x in shadow["positions"]], [("ABC", 168)])
        self.assertEqual(list(pd.read_csv(self.risk / "nav/nav_shadow.csv").date), [THURSDAY, FRIDAY, MONDAY])
        status = json.loads((self.risk / "risk_status.json").read_text())["run"]
        self.assertEqual(status["lastGoodAsOf"], MONDAY)


if __name__ == "__main__":
    unittest.main()
