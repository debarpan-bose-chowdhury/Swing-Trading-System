"""Sizer: risk-based targets, bucket budgets, funding limits and order, KEEP resizing, delta and DROP reasons."""

import unittest
from unittest.mock import ANY

import pandas as pd

from app.risk import ladder, monitor, sizer
from tests.app.risk_helpers import FRIDAY, Env

NAV = 700000.0
COOL = pd.DataFrame(columns=["ticker", "trigger_date", "trigger_adj_close", "release_after"])


def tgt(ticker="ABC", n=42000.0, close=250.0, s=0.18, adv_cap=1e12, bucket="SmallCap", rank=1, limited="NAME_CAP"):
    return {"ticker": ticker, "bucket": bucket, "rank": rank, "close": close, "adj": close, "s": s, "n": n, "limitedBy": limited, "advCap": adv_cap}


def pos(ticker="XYZ", qty=100, close=250.0, stop=205.0, bucket="SmallCap"):
    return {"ticker": ticker, "bucket": bucket, "qty": qty, "avg": close, "entry_date": "2026-01-05", "entry_source": "FILLS", "close": close,
            "adj": close, "prev_close": close, "has_row": True, "value": qty * close, "track_start": "2026-01-05", "track_source": "ENTRY_DATE",
            "stop": stop, "hwm": close, "breach": None}


class TargetTests(Env):
    def setUp(self):
        super().setUp()
        self.buckets(SmallCap=["ABC", "DEF"], MidCap=["MID"], LargeCap=[])
        for t in ("ABC", "DEF", "MID"):
            self.price(t)
        self.caps = ladder.caps(self.cfg, "BULL", 0)

    def build(self, selected, **kw):
        return sizer.targets(self.context(targets=self.targets(selected, **kw)), NAV, self.caps)

    def test_worked_example_168_shares(self):
        x, = self.build({"SmallCap": ["ABC"]})
        self.assertAlmostEqual(x["s"], 0.18)  # 3.5 x 3.6% = 12.6% -> lower clamp
        self.assertAlmostEqual(x["n"], 42000.0)  # 8,750 / 0.18 = 48,611 -> 6% name cap
        self.assertEqual((x["limitedBy"], int(x["n"] // x["close"])), ("NAME_CAP", 168))
        self.assertAlmostEqual(x["n"] * x["s"] / NAV, 0.0108)  # heat contribution 1.08% of NAV

    def test_risk_target_below_the_name_cap_is_kept(self):
        self.price("ABC", rng=40.0)  # 3.5 x 16% = 56% -> upper clamp 28%: N = 8,750 / 0.28 = 31,250
        x, = self.build({"SmallCap": ["ABC"]})
        self.assertAlmostEqual(x["n"], 31250.0)
        self.assertEqual(x["limitedBy"], "RISK_TARGET")

    def test_bucket_budget_scales_all_names_proportionally(self):
        xs = self.build({"SmallCap": ["ABC", "DEF"]}, comp={"LargeCap": 0, "MidCap": 0, "SmallCap": 0.1})  # budget 70,000 < 84,000
        self.assertEqual([round(x["n"]) for x in xs], [35000, 35000])
        self.assertEqual({x["limitedBy"] for x in xs}, {"BUCKET_BUDGET"})

    def test_unused_budget_stays_cash_with_the_analyst_placeholders(self):
        xs = self.build({"SmallCap": ["ABC", "DEF"]})  # weight 1, top_n 2
        self.assertAlmostEqual(sum(x["n"] for x in xs) / NAV, 0.12)  # 88% stays in cash

    def test_final_exposure_cap_shrinks_the_budget(self):
        caps = ladder.caps(self.cfg, "BULL", 3)  # 25% invested
        xs = sizer.targets(self.context(targets=self.targets({"SmallCap": ["ABC", "DEF", "MID"]}, top_n=3)), NAV, caps)
        self.assertLessEqual(sum(x["n"] for x in xs), NAV * 0.25 + 1e-6)

    def test_funding_order_is_bucket_weight_then_rank(self):
        xs = self.build({"SmallCap": ["DEF", "ABC"], "MidCap": ["MID"]}, comp={"LargeCap": 0, "MidCap": 0.6, "SmallCap": 0.4})
        self.assertEqual([x["ticker"] for x in xs], ["MID", "DEF", "ABC"])

    def test_names_without_a_row_on_asof_are_skipped_with_a_warning(self):
        self.price("ABC", days=self.days[:-1])
        ctx = self.context(targets=self.targets({"SmallCap": ["ABC", "DEF"]}))
        xs = sizer.targets(ctx, NAV, self.caps)
        self.assertEqual([x["ticker"] for x in xs], ["DEF"])
        self.assertIn("NO_ROW:ABC", ctx.warnings)

    def test_unknown_bucket_in_the_file_is_skipped(self):
        ctx = self.context(targets=self.targets({}, comp={"LargeCap": 0, "MidCap": 0, "SmallCap": 1, "NanoCap": 0}))
        sizer.targets(ctx, NAV, self.caps)
        self.assertIn("UNKNOWN_BUCKET:NanoCap", ctx.warnings)


class DropReasonTests(Env):
    def test_reasons(self):
        T = self.targets({"SmallCap": ["A"]}, top_n=2)
        self.assertEqual(sizer.drop_reason(T, "SmallCap"), "NOT_SELECTED")
        self.assertEqual(sizer.drop_reason(T, "MidCap"), "NO_ALLOCATION")  # weight 0
        self.assertEqual(sizer.drop_reason(T, "NanoCap"), "NO_ALLOCATION")  # missing from the file
        self.assertEqual(sizer.drop_reason(self.targets({}, top_n=0), "SmallCap"), "NO_ALLOCATION")  # top_n 0
        self.assertEqual(sizer.drop_reason(self.targets({}, strategy=False), "SmallCap"), "NO_ALLOCATION")  # no strategy
        self.assertEqual(sizer.drop_reason(self.targets({}, regime=("BULL", "Unknown")), "SmallCap"), "UNKNOWN_REGIME")
        self.assertEqual(sizer.drop_reason(self.targets({}, regime=("BULL", "Unknown")), "MidCap"), "UNKNOWN_REGIME")  # takes precedence


class FundingTests(Env):
    """sizer.buys with hand-built inputs: every limit, its reason code and the order."""

    def setUp(self):
        super().setUp()
        self.surveillance()
        self.ctx = self.context()
        self.caps = ladder.caps(self.cfg, "BULL", 0)

    def buys(self, tgts, held=(), sells=None, topups=None, cash=NAV, nav=NAV, caps=None, cool=COOL):
        return sizer.buys(self.ctx, list(held), sells or {}, cool, tgts, topups or {}, nav, cash, caps or self.caps)

    def blocked(self, tgts, **kw):
        _, blocked, _ = self.buys(tgts, **kw)
        return [(b["ticker"], b["intent"], b["reason"]) for b in blocked]

    def test_entry_quantity_notional_and_detail(self):
        (a,), blocked, fin = self.buys([tgt()])
        self.assertEqual((a["side"], a["kind"], a["qty"], a["notionalInr"], a["reason"], blocked), ("BUY", "ENTRY", 168, 42000.0, "ENTRY", []))
        self.assertEqual((a["detail"]["limitedBy"], a["detail"]["stopWidthPct"], a["detail"]["stopPriceAtEntry"], a["detail"]["rank"]), ("NAME_CAP", 0.18, 205.0, 1))
        self.assertAlmostEqual(fin["heatPct"], 0.0108)

    def test_quantity_is_rounded_down(self):
        (a,), _, _ = self.buys([tgt(n=42100.0)])
        self.assertEqual(a["qty"], 168)

    def test_zero_quantity(self):
        self.assertEqual(self.blocked([tgt(close=100000.0)]), [("ABC", "ENTRY", "ZERO_QTY")])

    def test_below_minimum_notional(self):
        self.assertEqual(self.blocked([tgt(n=21000.0)]), [("ABC", "ENTRY", "BELOW_MIN_NOTIONAL")])
        self.assertEqual(len(self.buys([tgt(n=25000.0)])[0]), 1)  # exactly the minimum is fine

    def test_adv_cap_limits_then_blocks(self):
        (a,), _, _ = self.buys([tgt(adv_cap=35000.0)])
        self.assertEqual((a["qty"], a["detail"]["limitedBy"]), (140, "ADV_CAP"))
        self.assertEqual(self.blocked([tgt(adv_cap=20000.0)]), [("ABC", "ENTRY", "ADV_CAP")])

    def test_cash_limit_reserves_the_buffer(self):
        # buffer 2% of 700,000 = 14,000: 50,000 cash leaves 36,000 for the first name, nothing worth buying for the second
        (a,), blocked, _ = self.buys([tgt(), tgt("DEF", rank=2)], cash=50000.0)
        self.assertEqual((a["ticker"], a["qty"], a["detail"]["limitedBy"]), ("ABC", 143, "INSUFFICIENT_CASH"))
        self.assertEqual([(b["ticker"], b["reason"]) for b in blocked], [("DEF", "INSUFFICIENT_CASH")])

    def test_sale_proceeds_fund_buys_when_enabled(self):
        held = [pos("XYZ", qty=2000, close=250.0)]
        sells = {"XYZ": {"qty": 2000, "reason": "STOP", "kind": "EXIT", "detail": {}, "priority": 1}}
        self.assertEqual(len(self.buys([tgt()], held=held, sells=sells, cash=10000.0)[0]), 1)
        self.cfg["sizing"]["countSaleProceeds"] = False
        self.assertEqual(self.blocked([tgt()], held=held, sells=sells, cash=10000.0), [("ABC", "ENTRY", "INSUFFICIENT_CASH")])

    def test_exposure_cap_room(self):
        held = [pos("XYZ", qty=2600, close=250.0, stop=240.0)]  # 650,000 invested of a 700,000 NAV, cap 100% -> 50,000 room
        (a,), _, _ = self.buys([tgt()], held=held, cash=50000.0 + 14000.0)
        self.assertEqual(a["qty"], 168)
        caps = ladder.caps(self.cfg, "BULL", 1)  # 75% cap = 525,000 < 650,000 invested: no room
        self.assertEqual(self.blocked([tgt()], held=held, caps=caps), [("ABC", "ENTRY", "EXPOSURE_CAP")])
        caps = {**self.caps, "finalCap": 0.95}  # room 15,000
        self.assertEqual(self.blocked([tgt()], held=held, caps=caps), [("ABC", "ENTRY", "EXPOSURE_CAP")])  # below the minimum order

    def test_heat_cap_scales_then_blocks(self):
        self.cfg["heat"]["capPct"] = 0.02  # 14,000: first name 7,560, 6,440 left for the second
        (a, b), _, fin = self.buys([tgt(), tgt("DEF", rank=2)])
        self.assertEqual((b["qty"], b["detail"]["limitedBy"]), (143, "HEAT_CAP"))
        self.cfg["heat"]["capPct"] = 0.011
        self.assertEqual(self.blocked([tgt(), tgt("DEF", rank=2)]), [("DEF", "ENTRY", "HEAT_CAP")])

    def test_existing_heat_uses_the_distance_to_the_ratcheted_stop(self):
        held = [pos("XYZ", qty=1000, close=250.0, stop=100.0)]  # 250,000 x 60% = 150,000 of heat: far above 12% x NAV
        self.assertEqual(self.blocked([tgt()], held=held), [("ABC", "ENTRY", "HEAT_CAP")])

    def test_cooldown_blocks_until_released_and_above_the_stop_close(self):
        cool = pd.DataFrame([{"ticker": "ABC", "trigger_date": "2026-09-10", "trigger_adj_close": "240", "release_after": "2026-09-25"}])
        self.assertEqual(self.blocked([tgt()], cool=cool), [("ABC", "ENTRY", "COOLDOWN")])  # not later than release_after
        cool["release_after"] = "2026-09-24"
        self.assertEqual(self.blocked([tgt(close=250.0)], cool=cool), [])  # released, 250 > 240
        cool["trigger_adj_close"] = "250"
        self.assertEqual(self.blocked([tgt(close=250.0)], cool=cool), [("ABC", "ENTRY", "COOLDOWN")])  # not above the stop-day close
        self.cfg["cooldown"]["reentryAboveStopClose"] = False
        self.assertEqual(self.blocked([tgt(close=250.0)], cool=cool), [])

    def test_cooldown_does_not_apply_to_a_topup(self):
        cool = pd.DataFrame([{"ticker": "XYZ", "trigger_date": "2026-09-10", "trigger_adj_close": "999", "release_after": "2026-12-31"}])
        (a,), _, _ = self.buys([tgt("XYZ", n=60000.0)], held=[pos("XYZ", qty=40, close=250.0)], topups={"XYZ": 50000.0}, cool=cool)
        self.assertEqual(a["kind"], "TOPUP")

    def test_surveillance_blocks_entries_and_topups(self):
        self.surveillance(gsm=["GSM1"], t2t=["T2T1"], asm={"ASM1": 1}, bands={"BND1": 5.0, "BND2": 20.0})
        self.ctx = self.context()
        tg = [tgt(t, rank=i) for i, t in enumerate(["GSM1", "T2T1", "ASM1", "BND1", "BND2", "OK1"], 1)]
        got = dict((t, r) for t, _, r in self.blocked(tg))
        self.assertEqual(got, {"GSM1": "SURVEILLANCE", "T2T1": "SURVEILLANCE", "ASM1": "SURVEILLANCE", "BND1": "SURVEILLANCE"})
        _, blocked, _ = self.buys([tgt("GSM1", n=60000.0)], held=[pos("GSM1", qty=40)], topups={"GSM1": 50000.0})
        self.assertEqual([(b["intent"], b["reason"]) for b in blocked], [("TOPUP", "SURVEILLANCE")])

    def test_missing_or_stale_list_blocks_every_buy(self):
        (self.risk / "surveillance" / f"surveillance_{FRIDAY}.json").unlink()
        self.ctx = self.context()
        self.assertEqual(self.blocked([tgt()]), [("ABC", "ENTRY", "NO_SURVEILLANCE_DATA")])
        _, blocked, _ = self.buys([tgt("XYZ", n=60000.0)], held=[pos("XYZ", qty=40)], topups={"XYZ": 50000.0})
        self.assertEqual(blocked[0]["reason"], "NO_SURVEILLANCE_DATA")

    def test_a_ticker_being_sold_gets_no_buy(self):
        held = [pos("ABC", qty=100)]
        sells = {"ABC": {"qty": 100, "reason": "STOP", "kind": "EXIT", "detail": {}, "priority": 1}}
        self.assertEqual(self.buys([tgt("ABC", n=60000.0)], held=held, sells=sells, topups={"ABC": 50000.0}), ([], [], ANY))

    def test_a_held_name_inside_the_band_gets_no_buy(self):
        a, b, _ = self.buys([tgt("XYZ")], held=[pos("XYZ", qty=100)])
        self.assertEqual((a, b), ([], []))

    def test_funding_is_first_come_first_served_in_the_given_order(self):
        (a,), blocked, _ = self.buys([tgt("MID", bucket="MidCap"), tgt("ABC", rank=2)], cash=70000.0)  # 56,000 available after the buffer
        self.assertEqual((a["ticker"], a["qty"]), ("MID", 168))
        self.assertEqual([b["ticker"] for b in blocked], ["ABC"])


class ResizeTests(Env):
    """KEEP resizing: the no-trade band and the minimum adjustment (TDD example: target 8%, 10.6% trimmed, 10.4% left)."""

    def setUp(self):
        super().setUp()
        self.buckets(MidCap=["MID"])
        self.price("MID")
        self.caps = ladder.caps(self.cfg, "BULL", 0)

    def resize(self, qty, nav=NAV):
        self.book([("MID", qty, 250.0, "2026-01-05")])
        ctx = self.context(targets=self.targets({"MidCap": ["MID"]}, comp={"LargeCap": 0, "MidCap": 1, "SmallCap": 0}))
        st = {"deferred": pd.DataFrame(columns=["ticker", "drop_date", "anniversary", "entry_date"])}
        held, _ = monitor.holdings(ctx, self.portfolio(cash=nav - qty * 250.0), {**st, "positions": pd.DataFrame(columns=["ticker", "bucket", "track_start", "track_source", "last_seen"]), "cooldown": COOL})
        tgts = sizer.targets(ctx, nav, self.caps)
        cands = monitor.new_cands()
        _, _, topups = sizer.plan_sells(ctx, held, st, cands, tgts, nav)
        return tgts[0], cands, topups

    def test_target_weight_is_8_percent(self):
        x, _, _ = self.resize(100)
        self.assertAlmostEqual(x["n"], 56000.0)  # MidCap name cap 8% of 700,000

    def test_overweight_outside_the_band_is_trimmed(self):
        _, cands, topups = self.resize(297)  # 74,250 = 10.6%: diff 2.6% > 2.5%
        c, = cands["MID"]
        self.assertEqual((c["reason"], c["qty"], topups), ("REBALANCE_TRIM", 73, {}))  # (74,250 - 56,000) / 250

    def test_overweight_inside_the_band_is_left(self):
        _, cands, topups = self.resize(291)  # 72,750 = 10.39%: diff 2.39% < 2.5%
        self.assertEqual((dict(cands), topups), ({}, {}))

    def test_underweight_outside_the_band_is_topped_up(self):
        _, cands, topups = self.resize(80)  # 20,000 = 2.9% vs 8%
        self.assertEqual((dict(cands), round(topups["MID"])), ({}, 36000))

    def test_gap_below_the_minimum_adjustment_is_ignored(self):
        self.cfg["sizing"]["minAdjustmentInr"] = 30000
        _, cands, topups = self.resize(297)  # trade value 18,250 < 30,000
        self.assertEqual((dict(cands), topups), ({}, {}))

    def test_a_ticker_already_selling_is_not_resized(self):
        self.book([("MID", 297, 250.0, "2026-01-05")])
        ctx = self.context(targets=self.targets({"MidCap": ["MID"]}, comp={"LargeCap": 0, "MidCap": 1, "SmallCap": 0}))
        st = {"deferred": pd.DataFrame(columns=["ticker", "drop_date", "anniversary", "entry_date"])}
        held, _ = monitor.holdings(ctx, self.portfolio(cash=625750.0), {**st, "positions": pd.DataFrame(columns=["ticker", "bucket", "track_start", "track_source", "last_seen"]), "cooldown": COOL})
        cands = monitor.new_cands()
        monitor.add(cands, "MID", "STOP", 297)
        _, _, topups = sizer.plan_sells(ctx, held, st, cands, sizer.targets(ctx, NAV, self.caps), NAV)
        self.assertEqual([c["reason"] for c in cands["MID"]], ["STOP"])


if __name__ == "__main__":
    unittest.main()
