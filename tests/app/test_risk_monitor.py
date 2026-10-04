"""Risk Monitor end to end: stops, late breaches, surveillance exits, exposure-cap reduction, ladder, deferral, state files."""

import json
import unittest

import numpy as np
import pandas as pd

from app.risk import monitor
from tests.app.risk_helpers import FRIDAY, THURSDAY, Env


def rising(n, lo=150.0, hi=250.0):
    return list(np.linspace(lo, hi, n))


class MonitorEnv(Env):
    def setUp(self):
        super().setUp()
        self.now = self.set_day(THURSDAY)
        self.buckets(SmallCap=["XYZ", "ABC", "DEF"], MidCap=["MID"], LargeCap=["LRG"])
        self.surveillance(day=THURSDAY)

    def px(self, ticker, closes, **kw):
        self.price(ticker, closes=closes, days=self.days_to(self.asof), **kw)

    def flat(self, ticker, base=300.0, last=None, **kw):
        n = len(self.days_to(self.asof))
        self.px(ticker, [base] * (n - 1) + [base if last is None else last], **kw)

    def go(self, **kw):
        report = self.run_risk(self.now, **kw)
        self.assertEqual(report.status(), "ok", report.error)
        return self.signals()

    def last_good(self, day):
        self.risk.mkdir(parents=True, exist_ok=True)
        (self.risk / "risk_status.json").write_text(json.dumps({"run": {"status": "ok", "lastGoodAsOf": day}}))

    def ladder_state(self, rung=0, peak=1.0, locked=False, since=None):
        (self.risk / "state").mkdir(parents=True, exist_ok=True)
        (self.risk / "state/ladder_state.json").write_text(json.dumps({
            "rung": rung, "peakIndex": peak, "peakDate": "2026-09-01", "baselineIndex": 1.0, "flatLocked": locked, "flatLockedSince": since,
            "lastRestartFrom": None, "lastReRiskWeek": None, "shadowStartDate": None}))

    def table(self, name, folder="state"):
        return pd.read_csv(self.risk / folder / f"{name}.csv", dtype=str, keep_default_na=False)


class StopTests(MonitorEnv):
    def test_close_at_or_below_the_trailing_stop_signals_a_full_exit(self):
        self.flat("XYZ", 300.0, last=245.0)
        self.book([("XYZ", 40, 280.0, "2026-01-05")])
        sig = self.go()
        a, = sig["actions"]
        self.assertEqual((a["side"], a["kind"], a["qty"], a["refPriceInr"], a["notionalInr"], a["reason"], a["priority"]), ("SELL", "EXIT", 40, 245.0, 9800.0, "STOP", 1))
        self.assertEqual((a["detail"]["stopPrice"], a["detail"]["breachDate"], a["detail"]["lateBreach"], a["detail"]["alsoTriggered"]), (246.0, THURSDAY, False, []))
        self.assertGreater(a["estChargesInr"], 0)
        p, = sig["positions"]
        self.assertEqual((p["stopPrice"], p["highWaterMark"], p["trackStart"]), (246.0, 300.0, "2026-01-05"))
        self.assertEqual(sig["executionDate"], "2026-09-25")

    def test_a_close_above_the_stop_is_no_signal(self):
        self.flat("XYZ", 300.0, last=250.0)
        self.book([("XYZ", 40, 280.0, "2026-01-05")])
        sig = self.go()
        self.assertEqual(sig["actions"], [])
        self.assertEqual(sig["positions"][0]["stopDistancePct"], round((250 - 246) / 250, 4))

    def test_stop_writes_a_cooldown_row_with_the_trigger_close(self):
        self.flat("XYZ", 300.0, last=245.0)
        self.book([("XYZ", 40, 280.0, "2026-01-05")])
        self.go()
        row = self.table("cooldown").iloc[0]
        self.assertEqual((row.ticker, row.trigger_date, float(row.trigger_adj_close), row.release_after), ("XYZ", THURSDAY, 245.0, "2026-10-09"))

    def test_stop_repeats_every_run_until_the_book_no_longer_holds_the_ticker(self):
        self.flat("XYZ", 300.0, last=245.0)
        self.book([("XYZ", 40, 280.0, "2026-01-05")])
        self.go()
        self.now = self.set_day(FRIDAY)
        self.targets({})  # Friday rebalance day, nothing selected
        self.surveillance()
        self.flat("XYZ", 300.0, last=300.0)  # recovered above the stop
        sig = self.go()
        a, = sig["actions"]
        self.assertEqual((a["reason"], a["detail"]["breachDate"], a["detail"]["alsoTriggered"]), ("STOP", THURSDAY, ["DROP_NOT_SELECTED"]))
        self.assertEqual(len(self.table("cooldown")), 1)  # the original trigger row is kept
        self.book([])  # sold: the signal stops
        self.assertEqual(self.go(force=True)["actions"], [])

    def test_late_breach_on_a_skipped_day_is_reported_as_a_stop(self):
        n = len(self.days_to(THURSDAY))
        self.px("XYZ", [300.0] * (n - 2) + [240.0, 300.0])  # Wed breach, Thu recovered
        self.book([("XYZ", 40, 280.0, "2026-01-05")])
        self.last_good("2026-09-22")
        a, = self.go()["actions"]
        self.assertEqual((a["reason"], a["detail"]["lateBreach"], a["detail"]["breachDate"]), ("STOP", True, "2026-09-23"))

    def test_a_breach_before_the_last_good_run_is_not_rescanned(self):
        n = len(self.days_to(THURSDAY))
        self.px("XYZ", [300.0] * (n - 2) + [240.0, 300.0])
        self.book([("XYZ", 40, 280.0, "2026-01-05")])
        self.last_good("2026-09-23")  # Wed was already evaluated by a good run
        self.assertEqual(self.go()["actions"], [])

    def test_unknown_entry_date_starts_the_track_at_the_first_run(self):
        n = len(self.days_to(THURSDAY))
        self.px("XYZ", [400.0] * (n - 5) + [300.0] * 5)  # fell from 400 before the first run: that history is ignored
        self.book([("XYZ", 40, 280.0, "UNKNOWN")])
        sig = self.go()
        self.assertEqual(sig["actions"], [])
        self.assertEqual(sig["positions"][0]["trackStart"], THURSDAY)
        row = self.table("positions").iloc[0]
        self.assertEqual((row.track_source, row.track_start, row.bucket, row.last_seen), ("FIRST_RUN", THURSDAY, "SmallCap", THURSDAY))
        # the stored start survives the next run
        self.now = self.set_day(FRIDAY)
        self.targets({"SmallCap": ["XYZ"]})
        self.surveillance()
        self.px("XYZ", [400.0] * (n - 4) + [300.0] * 5)
        self.assertEqual(self.go()["positions"][0]["trackStart"], THURSDAY)

    def test_entry_date_older_than_the_history_starts_at_the_first_row(self):
        self.flat("XYZ", 300.0)
        self.book([("XYZ", 40, 280.0, "2001-01-05")])
        self.assertEqual(self.go()["positions"][0]["trackStart"], "2001-01-05")  # reported as given; the replay clips to the first row
        self.assertEqual(self.table("positions").iloc[0].track_source, "ENTRY_DATE")

    def test_fewer_than_21_rows_uses_the_widest_clamp_and_warns(self):
        self.price("XYZ", closes=[300.0] * 15, days=self.days_to(THURSDAY)[-15:])
        self.book([("XYZ", 40, 280.0, "2026-09-01")])
        sig = self.go()
        self.assertEqual(sig["positions"][0]["stopPrice"], round(300 * 0.72, 2))  # SmallCap upper clamp 28%
        self.assertIn("ATR_UNAVAILABLE:XYZ", sig["warnings"])

    def test_held_ticker_without_a_row_is_valued_at_its_last_close_and_gets_no_action(self):
        others = [f"T{i}" for i in range(9)]  # one missing name of ten is exactly the 10% the gate tolerates
        for t in others:
            self.flat(t, 300.0)
        self.price("XYZ", closes=[300.0] * (len(self.days_to(THURSDAY)) - 1), days=self.days_to(THURSDAY)[:-1])  # last bar is Wednesday
        self.book([("XYZ", 40, 280.0, "2026-01-05")] + [(t, 10, 280.0, "2026-01-05") for t in others])
        sig = self.go()
        self.assertEqual(sig["actions"], [])
        self.assertEqual(sig["nav"]["positionsValueInr"], 12000.0 + 9 * 3000.0)
        self.assertIn("NO_ROW:XYZ", sig["warnings"])

    def test_bucket_comes_from_the_newest_bucket_file_then_the_stored_one_then_the_fallback(self):
        self.flat("MID", 300.0)
        self.flat("OLD", 300.0)
        self.flat("NEW", 300.0)
        self.book([("MID", 10, 280.0, "2026-01-05"), ("OLD", 10, 280.0, "2026-01-05"), ("NEW", 10, 280.0, "2026-01-05")])
        (self.risk / "state").mkdir(parents=True, exist_ok=True)
        pd.DataFrame([{"ticker": "OLD", "bucket": "LargeCap", "track_start": "2026-01-05", "track_source": "ENTRY_DATE", "last_seen": "2026-09-23"}]).to_csv(
            self.risk / "state" / "positions.csv", index=False)
        sig = self.go()
        buckets = {p["ticker"]: p["bucket"] for p in sig["positions"]}
        self.assertEqual(buckets, {"MID": "MidCap", "OLD": "LargeCap", "NEW": "SmallCap"})  # NEW: fallback
        self.assertIn("BUCKET_FALLBACK:NEW", sig["warnings"])
        self.assertNotIn("BUCKET_FALLBACK:OLD", sig["warnings"])

    def test_each_bucket_uses_its_own_clamp(self):
        self.flat("MID", 300.0)
        self.flat("LRG", 300.0)
        self.book([("MID", 10, 280.0, "2026-01-05"), ("LRG", 10, 280.0, "2026-01-05")])
        stops = {p["ticker"]: p["stopPrice"] for p in self.go()["positions"]}
        self.assertEqual(stops, {"MID": 300 * (1 - 0.14), "LRG": 300 - 3.5 * 9})  # MidCap: 10.5% is below its 14% floor; LargeCap: inside 10%-18%


class SurveillanceExitTests(MonitorEnv):
    def setUp(self):
        super().setUp()
        for t in ("XYZ", "ABC", "DEF"):
            self.flat(t, 300.0)
        self.book([("XYZ", 10, 280.0, "2026-01-05"), ("ABC", 10, 280.0, "2026-01-05"), ("DEF", 10, 280.0, "2026-01-05")])

    def test_gsm_and_trade_for_trade_force_exits(self):
        self.surveillance(day=THURSDAY, gsm=["XYZ"], t2t=["ABC"])
        acts = self.go()["actions"]
        self.assertEqual({a["ticker"]: (a["reason"], a["detail"]["list"], a["priority"], a["kind"]) for a in acts},
                         {"XYZ": ("SURVEILLANCE", "GSM", 2, "EXIT"), "ABC": ("SURVEILLANCE", "T2T", 2, "EXIT")})

    def test_asm_and_tight_bands_on_held_names_only_warn(self):
        self.surveillance(day=THURSDAY, asm={"XYZ": 2}, bands={"ABC": 5.0})
        sig = self.go()
        self.assertEqual(sig["actions"], [])
        self.assertTrue({"ASM:XYZ", "TIGHT_BAND:ABC"} <= set(sig["warnings"]))

    def test_stale_list_up_to_3_trading_days_still_forces_exits(self):
        (self.risk / "surveillance" / f"surveillance_{THURSDAY}.json").unlink()
        self.surveillance(day="2026-09-21", gsm=["XYZ"])  # Mon, Thu is 3 trading days later
        a, = self.go()["actions"]
        self.assertEqual(a["ticker"], "XYZ")

    def test_a_list_older_than_3_trading_days_is_ignored(self):
        (self.risk / "surveillance" / f"surveillance_{THURSDAY}.json").unlink()
        self.surveillance(day="2026-09-18", gsm=["XYZ"])
        sig = self.go()
        self.assertEqual(sig["actions"], [])
        self.assertEqual(sig["surveillance"]["status"], "stale")

    def test_stop_outranks_surveillance(self):
        self.flat("XYZ", 300.0, last=245.0)
        self.surveillance(day=THURSDAY, gsm=["XYZ"])
        a = self.go()["actions"][0]
        self.assertEqual((a["ticker"], a["reason"], a["detail"]["alsoTriggered"]), ("XYZ", "STOP", ["SURVEILLANCE"]))

    def test_lower_circuit_likely_when_the_day_return_reaches_the_band(self):
        n = len(self.days_to(THURSDAY))
        self.px("XYZ", [300.0] * (n - 1) + [285.0])  # -5%
        self.px("ABC", [300.0] * (n - 1) + [286.0])  # -4.67%
        self.surveillance(day=THURSDAY, bands={"XYZ": 5.0, "ABC": 5.0, "DEF": 5.0})
        warnings = self.go()["warnings"]
        self.assertIn("LOWER_CIRCUIT_LIKELY:XYZ", warnings)
        self.assertNotIn("LOWER_CIRCUIT_LIKELY:ABC", warnings)  # -4.67% is above -(5 - 0.1)%


class CapReductionTests(MonitorEnv):
    """Final cap = min(regime cap, ladder cap); sell highest risk first until the invested value fits."""

    def hold(self, **qty):
        for t in qty:
            self.flat(t, 250.0)
        total = sum(q * 250.0 for q in qty.values())
        self.flows([("2026-01-01", "OPENING", 700000 - total)])
        self.book([(t, q, 250.0, "2026-01-05") for t, q in qty.items()])

    def bear(self, cap):
        self.regimes([(THURSDAY, "BEAR", "BEAR")])
        self.cfg["exposure"]["regimeCap"]["BEAR"] = cap

    def test_whole_names_then_a_trim_of_the_next(self):
        self.hold(XYZ=400, ABC=800, DEF=1200)  # 100k, 200k, 300k: 600k invested
        self.bear(0.25)  # cap 175k: excess 425k
        acts = {a["ticker"]: a for a in self.go()["actions"]}
        self.assertEqual((acts["DEF"]["kind"], acts["DEF"]["qty"], acts["DEF"]["reason"]), ("EXIT", 1200, "REGIME_CAP"))
        self.assertEqual((acts["ABC"]["kind"], acts["ABC"]["qty"]), ("TRIM", 500))  # 125k excess left / 250
        self.assertNotIn("XYZ", acts)
        self.assertEqual(acts["DEF"]["priority"], 3)

    def test_ladder_reason_when_the_ladder_cap_binds(self):
        self.hold(DEF=1200)  # 300k
        self.bear(0.5)
        self.ladder_state(rung=3)
        a, = self.go()["actions"]  # ladder rung 3 = 25% of 700k = 175k
        self.assertEqual((a["reason"], a["kind"], a["qty"]), ("LADDER", "TRIM", 500))  # (300k - 175k) / 250
        self.assertEqual(a["detail"]["rung"], 3)

    def test_residual_below_the_minimum_adjustment_is_not_traded(self):
        self.hold(DEF=1200)  # 300k
        self.bear(300000 / 700000 - 0.005)  # excess 3,500 < Rs 10,000
        sig = self.go()
        self.assertEqual(sig["actions"], [])
        self.assertTrue(any(w.startswith("CAP_NOT_REACHED") for w in sig["warnings"]))

    def test_a_position_already_selling_is_not_counted_or_reduced_again(self):
        self.hold(XYZ=400, ABC=800)
        self.flat("XYZ", 300.0, last=200.0)  # stopped out
        self.surveillance(day=THURSDAY)
        self.bear(0.5)  # remaining ABC 200k is within the 350k cap
        acts = {a["ticker"]: a["reason"] for a in self.go()["actions"]}
        self.assertEqual(acts, {"XYZ": "STOP"})

    def test_largest_risk_contribution_goes_first_ties_small_cap_first(self):
        mk = lambda t, b, value=100000.0: {"ticker": t, "bucket": b, "qty": 400, "value": value, "close": 250.0, "adj": 250.0, "stop": 200.0, "has_row": True}  # noqa: E731
        ctx = self.context()
        held = [mk("ZZZ", "LargeCap"), mk("BBB", "SmallCap"), mk("AAA", "SmallCap"), mk("MMM", "MidCap"), mk("BIG", "LargeCap", 200000.0)]
        cands = monitor.new_cands()
        monitor.reduce_to_cap(ctx, held, {"finalCap": 0.0, "reason": "LADDER"}, 700000.0, cands, {})
        order = list(cands)
        self.assertEqual(order, ["BIG", "AAA", "BBB", "MMM", "ZZZ"])  # biggest first; then SmallCap, MidCap, LargeCap; ticker ascending

    def test_names_that_cannot_be_sold_leave_a_residual_that_is_reported(self):
        ctx = self.context()
        held = [{"ticker": "NOROW", "bucket": "SmallCap", "qty": 100, "value": 100000.0, "close": 250.0, "adj": 250.0, "stop": None, "has_row": False}]
        monitor.reduce_to_cap(ctx, held, {"finalCap": 0.0, "reason": "LADDER"}, 700000.0, monitor.new_cands(), {})
        self.assertIn("CAP_NOT_REACHED:100000", ctx.warnings)

    def test_flat_ladder_sells_everything(self):
        self.hold(XYZ=400, ABC=800)
        self.ladder_state(rung=4, locked=True, since="2026-09-10")
        sig = self.go()
        self.assertEqual({a["ticker"]: (a["kind"], a["reason"]) for a in sig["actions"]}, {"XYZ": ("EXIT", "LADDER"), "ABC": ("EXIT", "LADDER")})
        self.assertTrue(sig["ladder"]["flatLocked"])


class LadderRunTests(MonitorEnv):
    def test_drawdown_on_the_time_weighted_index_steps_the_ladder_and_cuts_exposure(self):
        self.flat("DEF", 250.0)
        self.flows([("2026-01-01", "OPENING", 100000)])
        self.book([("DEF", 2400, 250.0, "2026-01-05")])  # 600k + 100k = 700k NAV
        self.ladder_state()
        nav_dir = self.risk / "nav"
        nav_dir.mkdir(parents=True)
        pd.DataFrame([{"date": "2026-09-23", "positions_value": 700000, "cash": 100000, "nav": 800000, "flow": 0, "twr_index": 1.0, "bench_close": 20000, "active_regime": "BULL", "rung": 0}]
                     ).to_csv(nav_dir / "nav_actual.csv", index=False)  # NAV fell 12.5%: rung 1 = 75%
        sig = self.go()
        self.assertEqual((sig["nav"]["drawdownPct"], sig["ladder"]["rung"], sig["exposure"]["ladderCap"]), (0.125, 1, 0.75))
        a, = sig["actions"]
        self.assertEqual((a["reason"], a["kind"], a["qty"], a["detail"]["rung"]), ("LADDER", "TRIM", 300, 1))  # (600k - 525k) / 250
        self.assertEqual(json.loads((self.risk / "state/ladder_state.json").read_text())["rung"], 1)

    def test_a_deposit_is_not_a_gain_and_a_withdrawal_is_not_a_loss(self):
        self.flat("DEF", 250.0)
        self.flows([("2026-01-01", "OPENING", 0), ("2026-09-24", "DEPOSIT", 100000)])
        self.book([("DEF", 400, 250.0, "2026-01-05")])  # NAV 200k after the deposit
        self.ladder_state()
        nav_dir = self.risk / "nav"
        nav_dir.mkdir(parents=True)
        pd.DataFrame([{"date": "2026-09-23", "positions_value": 100000, "cash": 0, "nav": 100000, "flow": 0, "twr_index": 1.0, "bench_close": 20000, "active_regime": "BULL", "rung": 0}]
                     ).to_csv(nav_dir / "nav_actual.csv", index=False)
        sig = self.go()
        self.assertEqual((sig["nav"]["twrIndex"], sig["nav"]["drawdownPct"]), (1.0, 0.0))
        self.assertNotIn("NAV_JUMP", sig["warnings"])


class DeferralTests(MonitorEnv):
    """Rank-based DROPs near 12 months are deferred; every other exit reason overrides the deferral."""

    def setUp(self):
        super().setUp()
        self.now = self.set_day(FRIDAY)
        self.surveillance()
        self.n = len(self.days)

    def friday(self, entry="2025-10-10", avg=200.0, closes=None, selected=None, **kw):
        self.px_f("XYZ", closes if closes is not None else rising(self.n))
        self.book([("XYZ", 40, avg, entry)])
        self.targets(selected or {}, **kw)
        return self.go(force=True)

    def px_f(self, ticker, closes):
        self.price(ticker, closes=closes)

    def test_deferred_when_within_28_days_gain_10_percent_and_above_trend(self):
        sig = self.friday()
        self.assertEqual(sig["actions"], [])
        h, = sig["holds"]
        self.assertEqual((h["ticker"], h["action"], h["release"], h["unrealisedGainPct"]), ("XYZ", "HOLD_DEFERRED", "2026-10-10", 0.25))
        row = self.table("deferred_drops").iloc[0]
        self.assertEqual((row.ticker, row.drop_date, row.anniversary, row.entry_date), ("XYZ", FRIDAY, "2026-10-10", "2025-10-10"))

    def test_deferred_exactly_28_days_before_and_the_day_before(self):
        self.assertEqual(len(self.friday(entry="2025-10-23")["holds"]), 1)  # anniversary 2026-10-23, 28 days
        self.assertEqual(len(self.friday(entry="2025-09-26")["holds"]), 1)  # 1 day

    def test_not_deferred_outside_the_window(self):
        a, = self.friday(entry="2025-10-24")["actions"]  # 29 days
        self.assertEqual((a["reason"], a["kind"]), ("DROP_NOT_SELECTED", "EXIT"))
        a, = self.friday(entry="2025-09-25")["actions"]  # anniversary is today: no new deferral
        self.assertEqual(a["reason"], "DROP_NOT_SELECTED")

    def test_not_deferred_when_the_gain_is_below_10_percent(self):
        a, = self.friday(avg=227.5)["actions"]  # 250 / 227.5 - 1 = 9.89%
        self.assertEqual(a["reason"], "DROP_NOT_SELECTED")

    def test_not_deferred_with_an_unknown_entry_date(self):
        a, = self.friday(entry="UNKNOWN")["actions"]
        self.assertEqual(a["reason"], "DROP_NOT_SELECTED")

    def test_not_deferred_below_the_trend_ma(self):
        a, = self.friday(closes=[255.0] * (self.n - 5) + [250.0] * 5)["actions"]  # MA150 ~254 > 250
        self.assertEqual(a["reason"], "DROP_NOT_SELECTED")
        self.assertEqual(self.signals()["holds"], [])

    def test_trend_window_comes_from_the_target_file(self):
        a, = self.friday(closes=[255.0] * (self.n - 5) + [250.0] * 5, ma=3)["actions"]  # MA3 = 250 -> not above
        self.assertEqual(a["reason"], "DROP_NOT_SELECTED")
        self.assertEqual(len(self.friday(closes=[240.0] * (self.n - 1) + [250.0], ma=3)["holds"]), 1)  # price above MA3

    def test_no_deferral_when_the_bucket_window_is_unknown(self):
        a, = self.friday(strategy=False)["actions"]  # no strategy -> NO_ALLOCATION anyway
        self.assertEqual(a["reason"], "DROP_NO_ALLOCATION")

    def test_deferral_can_be_switched_off(self):
        self.cfg["tax"]["deferral"]["enabled"] = False
        self.assertEqual(self.friday()["actions"][0]["reason"], "DROP_NOT_SELECTED")

    def test_no_allocation_and_unknown_regime_are_never_deferred(self):
        a, = self.friday(comp={"LargeCap": 0, "MidCap": 1, "SmallCap": 0})["actions"]
        self.assertEqual(a["reason"], "DROP_NO_ALLOCATION")
        a, = self.friday(regime=("BULL", "Unknown"))["actions"]
        self.assertEqual(a["reason"], "DROP_UNKNOWN_REGIME")

    def test_stop_overrides_the_deferral(self):
        n = self.n
        sig = self.friday(closes=rising(n - 1) + [150.0])
        a, = sig["actions"]
        self.assertEqual((a["reason"], a["detail"]["alsoTriggered"]), ("STOP", ["DROP_NOT_SELECTED"]))
        self.assertEqual((sig["holds"], len(self.table("deferred_drops"))), ([], 0))

    def test_a_new_friday_list_selecting_the_ticker_makes_it_a_keep_and_clears_the_row(self):
        self.friday()
        self.assertEqual(len(self.table("deferred_drops")), 1)
        sig = self.friday(selected={"SmallCap": ["XYZ"]})
        self.assertEqual((sig["holds"], len(self.table("deferred_drops"))), ([], 0))

    def test_surveillance_exit_overrides_the_deferral(self):
        self.surveillance(gsm=["XYZ"])
        a, = self.friday()["actions"]
        self.assertEqual((a["reason"], a["detail"]["alsoTriggered"]), ("SURVEILLANCE", ["DROP_NOT_SELECTED"]))

    def test_cap_reduction_overrides_the_deferral(self):
        self.regimes([(FRIDAY, "BEAR", "BEAR")])
        self.cfg["exposure"]["regimeCap"]["BEAR"] = 0.0
        a, = self.friday(regime=("BEAR", "BEAR"))["actions"]
        self.assertEqual((a["reason"], a["detail"]["alsoTriggered"]), ("REGIME_CAP", ["DROP_NOT_SELECTED"]))


class DeferredDailyTests(MonitorEnv):
    """Rows in deferred_drops.csv are re-checked on every run, not only on Fridays."""

    def setUp(self):
        super().setUp()
        self.targets(day="2026-09-18")  # the newest earlier target file supplies the trend window
        n = len(self.days_to(THURSDAY))
        self.px("XYZ", rising(n))
        self.book([("XYZ", 40, 200.0, "2025-10-10")])
        (self.risk / "state").mkdir(parents=True, exist_ok=True)
        self.row("2026-10-10")

    def row(self, anniversary, ticker="XYZ", entry="2025-10-10"):
        pd.DataFrame([{"ticker": ticker, "drop_date": "2026-09-18", "anniversary": anniversary, "entry_date": entry}]).to_csv(self.risk / "state/deferred_drops.csv", index=False)

    def test_still_deferred_before_the_anniversary(self):
        sig = self.go()
        self.assertEqual((sig["actions"], [h["ticker"] for h in sig["holds"]], sig["weekly"]["included"]), ([], ["XYZ"], False))
        self.assertEqual(len(self.table("deferred_drops")), 1)

    def test_released_when_asof_reaches_the_anniversary(self):
        self.row("2026-09-24")
        a, = self.go()["actions"]
        self.assertEqual((a["reason"], a["kind"], a["qty"], a["detail"]["release"]), ("DROP_DEFERRED_RELEASED", "EXIT", 40, "2026-09-24"))
        self.assertEqual(len(self.table("deferred_drops")), 0)

    def test_the_day_before_the_anniversary_still_holds(self):
        self.row("2026-09-25")
        self.assertEqual(len(self.go()["holds"]), 1)

    def test_sold_when_the_close_drops_to_the_trend_ma(self):
        n = len(self.days_to(THURSDAY))
        self.px("XYZ", [255.0] * (n - 5) + [250.0] * 5)
        a, = self.go()["actions"]
        self.assertEqual((a["reason"], a["detail"]["deferralEnded"]), ("DROP_NOT_SELECTED", "BELOW_TREND"))

    def test_a_deferred_name_without_a_row_today_keeps_its_deferral(self):
        n = len(self.days_to(THURSDAY))
        self.price("XYZ", closes=rising(n - 1), days=self.days_to(THURSDAY)[:-1])
        others = [f"T{i}" for i in range(9)]
        for t in others:
            self.price(t, days=self.days_to(THURSDAY))
        self.book([("XYZ", 40, 200.0, "2025-10-10")] + [(t, 1, 250.0, "2026-01-05") for t in others])
        sig = self.go()
        self.assertEqual((sig["actions"], len(self.table("deferred_drops"))), ([], 1))

    def test_row_of_a_ticker_no_longer_held_disappears(self):
        self.book([])
        self.go()
        self.assertEqual(len(self.table("deferred_drops")), 0)


if __name__ == "__main__":
    unittest.main()
