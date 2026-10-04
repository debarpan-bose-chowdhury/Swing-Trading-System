"""Evaluator statistics, tax estimate and the Evaluate stage."""

import argparse
import json
import math
import unittest
from datetime import datetime

import numpy as np
import pandas as pd

from app.analyst.journal import COLS as JOURNAL_COLS
from app.market.common import IST
from app.risk import common, evaluate, evaluator, tax
from app.risk.common import Gate, Report
from tests.app.risk_helpers import FRIDAY, LOG, THURSDAY, Env


def series(values, start="2026-01-05"):
    days = [d.date().isoformat() for d in pd.bdate_range(start, periods=len(values))]
    return pd.Series(values, index=days, dtype=float)


class PerfTests(unittest.TestCase):
    def test_total_return_volatility_and_drawdown(self):
        r = series([0.10, -0.10, 0.05, 0.0])
        p = evaluator.perf(r, 0.0)
        self.assertAlmostEqual(p["totalReturn"], 1.1 * 0.9 * 1.05 - 1, places=6)
        self.assertAlmostEqual(p["volatility"], r.std(ddof=1) * math.sqrt(252), places=5)
        self.assertAlmostEqual(p["maxDrawdown"], -0.10, places=6)

    def test_drawdown_duration_runs_from_the_peak_to_recovery(self):
        curve = pd.Series([100, 90, 95, 100, 101, 99], index=["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09", "2026-01-12"], dtype=float)
        self.assertEqual(evaluator.drawdown_days(curve), 3)  # 5 Jan peak, recovered on the 8th
        unrecovered = curve.iloc[:3]
        self.assertEqual(evaluator.drawdown_days(unrecovered), 2)  # still under water on the 7th
        self.assertEqual(evaluator.drawdown_days(curve.iloc[:1]), 0)

    def test_drawdown_duration_uses_the_longest_spell(self):
        idx = [f"2026-01-{d:02d}" for d in (5, 6, 7, 8, 9, 12, 13, 14)]
        curve = pd.Series([100, 99, 101, 100, 99, 98, 99, 102], index=idx, dtype=float)
        self.assertEqual(evaluator.drawdown_days(curve), 7)  # peak 7 Jan, recovered 14 Jan; the 8-9 Jan dip was shorter

    def test_sharpe_sortino_cvar_and_ulcer(self):
        rng = np.random.default_rng(1)
        r = series(rng.normal(0.001, 0.01, 300))
        p = evaluator.perf(r, 0.055)
        ex = r - 0.055 / 252
        self.assertAlmostEqual(p["sharpe"], ex.mean() / r.std() * math.sqrt(252), places=5)
        down = math.sqrt((np.minimum(ex, 0) ** 2).mean())
        self.assertAlmostEqual(p["sortino"], ex.mean() / down * math.sqrt(252), places=5)
        self.assertAlmostEqual(p["cvar95"], r[r <= r.quantile(0.05)].mean(), places=6)
        self.assertGreater(p["ulcerIndex"], 0)
        self.assertAlmostEqual(p["calmar"], p["cagr"] / abs(p["maxDrawdown"]), places=4)

    def test_short_series_reports_nothing_instead_of_crashing(self):
        self.assertEqual(evaluator.perf(series([0.01]), 0.05), {})
        self.assertEqual(evaluator.versus(series([0.01, 0.02]), series([0.01, 0.02]), 0.05), {})

    def test_beta_alpha_and_capture(self):
        b = series([0.01, -0.02, 0.015, -0.01, 0.02, -0.005, 0.01, -0.015])
        v = evaluator.versus(b * 2, b, 0.0)
        self.assertAlmostEqual(v["beta"], 2.0, places=5)
        self.assertAlmostEqual(v["alphaAnnual"], 0.0, places=5)
        self.assertAlmostEqual(v["upCapture"], 2.0, places=5)
        self.assertAlmostEqual(v["downCapture"], 2.0, places=5)
        v = evaluator.versus(b + 0.001, b, 0.0)
        self.assertAlmostEqual(v["alphaAnnual"], 0.001 * 252, places=4)

    def test_xirr(self):
        self.assertAlmostEqual(evaluator.xirr([("2025-01-01", -1000.0), ("2026-01-01", 1100.0)]), 0.10, places=4)
        self.assertIsNone(evaluator.xirr([("2025-01-01", -1000.0), ("2026-01-01", -1100.0)]))
        # a deposit mid-way: 1000 grows to 1100, then 1000 more, ends at 2310 after another 10%
        self.assertAlmostEqual(evaluator.xirr([("2025-01-01", -1000.0), ("2026-01-01", -1000.0), ("2027-01-01", 2310.0)]), 0.10, places=2)

    def test_compound_return(self):
        self.assertAlmostEqual(evaluator.compound(series([0.1, 0.1])), 0.21)
        self.assertIsNone(evaluator.compound(series([])))


class WindowTests(Env):
    def frame(self, n=100, bear_from=60):
        idx = [d.date().isoformat() for d in pd.bdate_range("2026-01-05", periods=n)]
        rng = np.random.default_rng(3)
        df = pd.DataFrame({"actual": rng.normal(0.001, 0.01, n), "bench": rng.normal(0.0005, 0.01, n), "shadow": rng.normal(0.001, 0.01, n),
                           "regime": ["BULL"] * min(bear_from, n) + ["BEAR"] * max(n - bear_from, 0)}, index=idx)
        return df

    def navs(self, df):
        return pd.DataFrame({"date": df.index, "nav": 1000.0, "flow": 0.0, "positions_value": 500.0})

    def test_bear_period_statistics(self):
        df = self.frame()
        rep = evaluator.window_report(df, self.navs(df), self.cfg)
        bear = df[df.regime == "BEAR"]
        self.assertEqual(rep["bear"]["bearDays"], 40)
        self.assertAlmostEqual(rep["bear"]["strategyCompound"], round((1 + bear.actual).prod() - 1, 6), places=5)
        self.assertAlmostEqual(rep["bear"]["benchmarkCompound"], round((1 + bear.bench).prod() - 1, 6), places=5)
        self.assertEqual((rep["bear"]["negativeDaysStrategy"], rep["bear"]["negativeDaysBenchmark"]), (int((df.actual < 0).sum()), int((df.bench < 0).sum())))

    def test_regime_table_and_dominant_regime(self):
        rep = evaluator.window_report(self.frame(), self.navs(self.frame()), self.cfg)["regime"]
        self.assertEqual(rep["dominant"], "BULL")
        self.assertEqual((rep["byRegime"]["BULL"]["days"], rep["byRegime"]["BEAR"]["days"]), (60, 40))
        self.assertEqual(set(rep["byRegime"]["BEAR"]), {"days", "return", "volatility", "maxDrawdown"})

    def test_low_sample_flag_under_min_obs(self):
        df = self.frame(n=59)
        self.assertEqual(evaluator.window_report(df, self.navs(df), self.cfg)["flags"], ["LOW_SAMPLE"])
        df = self.frame(n=60)
        self.assertEqual(evaluator.window_report(df, self.navs(df), self.cfg)["flags"], [])

    def test_tracking_gap_is_actual_minus_shadow_total_return(self):
        df = self.frame()
        rep = evaluator.window_report(df, self.navs(df), self.cfg)
        self.assertAlmostEqual(rep["trackingGap"], round(evaluator.compound(df.actual) - evaluator.compound(df.shadow), 6), places=5)

    def test_windows_split_by_financial_and_calendar_year(self):
        idx = ["2026-03-30", "2026-03-31", "2026-04-01", "2026-04-02", "2027-01-04"]
        df = pd.DataFrame({"actual": 0.01, "bench": 0.01, "shadow": 0.01, "regime": "BULL"}, index=idx)
        w = evaluator.windows(df)
        self.assertEqual(set(w), {"inception", "trailing252", "fy:2025-26", "fy:2026-27", "cy:2026", "cy:2027"})
        self.assertEqual((len(w["fy:2025-26"]), len(w["fy:2026-27"]), len(w["cy:2026"])), (2, 3, 4))

    def test_xirr_is_used_when_the_window_has_flows(self):
        df = self.frame(n=70)
        navs = pd.DataFrame({"date": df.index, "nav": np.linspace(1000, 1300, 70), "flow": 0.0, "positions_value": 500.0})
        navs.loc[30, "flow"] = 100.0
        with_flow = evaluator.window_report(df, navs, self.cfg)["actual"]["cagr"]
        without = evaluator.window_report(df, navs.assign(flow=0.0), self.cfg)["actual"]["cagr"]
        self.assertNotEqual(with_flow, without)


class JournalEnv(Env):
    def journal(self, rows):
        out = []
        for i, (ticker, entry, exit_, entry_price, exit_price, qty, source) in enumerate(rows):
            net = (exit_price - entry_price) * qty - 10
            out.append({**dict.fromkeys(JOURNAL_COLS, ""), "trade_id": f"{ticker}-{exit_}-{i}", "ticker": ticker, "qty": qty, "entry_date": entry, "entry_price": entry_price,
                        "exit_date": exit_, "exit_price": exit_price, "net_pl": net, "pl": net + 10, "est_charges": 10, "source": source})
        return pd.DataFrame(out, columns=JOURNAL_COLS).astype(str)


class TaxTests(JournalEnv):
    RATES = {"asOf": "2026-10-02", "stcgPct": 0.20, "ltcgPct": 0.125, "ltcgExemptionInr": 125000, "cessPct": 0.04}
    NAVS = pd.DataFrame({"date": ["2026-05-01", "2026-06-01"], "nav": [100000.0, 200000.0]})

    def est(self, rows, positions=(), asof="2026-10-02"):
        return tax.estimate(self.journal(rows), self.RATES, self.NAVS, list(positions), asof)

    def test_financial_year_labels(self):
        self.assertEqual([tax.fy(d) for d in ("2026-03-31", "2026-04-01", "2026-12-31", "2027-03-31")], ["2025-26", "2026-27", "2026-27", "2026-27"])

    def test_short_and_long_term_split_at_12_months(self):
        self.assertFalse(tax.holding_is_long("2025-05-01", "2026-05-01"))  # exactly 12 months: still short-term
        self.assertTrue(tax.holding_is_long("2025-05-01", "2026-05-02"))
        self.assertFalse(tax.holding_is_long("2024-02-29", "2025-02-28"))

    def test_short_term_gain_is_taxed_at_20_percent_plus_cess(self):
        r = self.est([("A", "2026-04-10", "2026-05-10", 100.0, 200.0, 1000, "FILLS")])["2026-27"]  # net 99,990
        self.assertAlmostEqual(r["netShortTermGainInr"], 99990.0)
        self.assertAlmostEqual(r["stcgTaxInr"], 19998.0)
        self.assertAlmostEqual(r["cessInr"], 19998.0 * 0.04, places=1)
        self.assertAlmostEqual(r["estimatedTaxInr"], 19998.0 * 1.04, places=1)
        self.assertAlmostEqual(r["postTaxPlInr"], 99990.0 - 19998.0 * 1.04, places=1)
        self.assertAlmostEqual(r["postTaxPctOfAvgNav"], r["postTaxPlInr"] / 150000.0, places=4)
        self.assertTrue(r["estimate"] and "not tax advice" in r["note"])

    def test_long_term_gain_is_taxed_only_above_the_1_25_lakh_exemption(self):
        r = self.est([("A", "2024-04-10", "2026-05-10", 100.0, 400.0, 600, "FILLS")])["2026-27"]  # net 179,990
        self.assertAlmostEqual(r["netLongTermGainInr"], 179990.0)
        self.assertAlmostEqual(r["ltcgTaxInr"], (179990 - 125000) * 0.125, places=2)
        small = self.est([("A", "2024-04-10", "2026-05-10", 100.0, 200.0, 1000, "FILLS")])["2026-27"]  # net 99,990 under the exemption
        self.assertEqual((small["ltcgTaxInr"], small["estimatedTaxInr"]), (0.0, 0.0))

    def test_short_term_loss_offsets_short_and_long_term_gains(self):
        r = self.est([("A", "2026-04-10", "2026-05-10", 100.0, 150.0, 1000, "FILLS"),   # ST +49,990
                      ("B", "2026-04-10", "2026-05-11", 100.0, 70.0, 1000, "FILLS"),    # ST -30,010
                      ("C", "2024-04-10", "2026-05-12", 100.0, 400.0, 600, "FILLS")])["2026-27"]  # LT +179,990
        self.assertAlmostEqual(r["netShortTermGainInr"], 19980.0)
        self.assertAlmostEqual(r["stcgTaxInr"], 3996.0)
        self.assertAlmostEqual(r["ltcgTaxInr"], (179990 - 125000) * 0.125, places=2)

    def test_short_term_loss_beyond_short_gains_reduces_long_term_gain(self):
        r = self.est([("B", "2026-04-10", "2026-05-11", 100.0, 20.0, 1000, "FILLS"),     # ST -80,010
                      ("C", "2024-04-10", "2026-05-12", 100.0, 400.0, 1000, "FILLS")])["2026-27"]  # LT +299,990
        self.assertAlmostEqual(r["netLongTermGainInr"], 299990 - 80010)
        self.assertEqual(r["stcgTaxInr"], 0.0)

    def test_long_term_loss_never_offsets_short_term_gain(self):
        r = self.est([("A", "2026-04-10", "2026-05-10", 100.0, 200.0, 1000, "FILLS"),    # ST +99,990
                      ("C", "2024-04-10", "2026-05-12", 400.0, 100.0, 1000, "FILLS")])["2026-27"]  # LT -300,010
        self.assertAlmostEqual(r["stcgTaxInr"], 99990 * 0.2)
        self.assertEqual(r["ltcgTaxInr"], 0.0)
        self.assertAlmostEqual(r["realisedNetPlInr"], 99990 - 300010)

    def test_unknown_entry_dates_and_unverified_rows_are_excluded_and_listed(self):
        reports = self.est([("A", "UNKNOWN", "2026-05-10", 100.0, 200.0, 1000, "FILLS"), ("B", "2026-04-10", "2026-05-10", 100.0, 200.0, 1000, "ESTIMATED"),
                            ("C", "2026-04-10", "2026-05-10", 100.0, 110.0, 100, "MANUAL_VERIFIED")])
        r = reports["2026-27"]
        self.assertEqual(r["excludedUnknownEntryDate"], ["A-2026-05-10-0"])
        self.assertAlmostEqual(r["netShortTermGainInr"], 990.0)  # only C counts

    def test_each_financial_year_has_its_own_report(self):
        reports = self.est([("A", "2025-12-10", "2026-03-10", 100.0, 150.0, 100, "FILLS"), ("B", "2026-04-10", "2026-05-10", 100.0, 150.0, 100, "FILLS")])
        self.assertEqual(sorted(reports), ["2025-26", "2026-27"])
        self.assertAlmostEqual(reports["2025-26"]["netShortTermGainInr"], 4990.0)

    def test_current_year_report_exists_without_trades_and_lists_positions_near_12_months(self):
        positions = [{"ticker": "NEAR", "entry_date": "2025-11-25", "avg": 100.0, "close": 130.0},   # 54 days to the anniversary
                     {"ticker": "EDGE", "entry_date": "2025-11-27", "avg": 100.0, "close": 90.0},   # exactly 56 days: included
                     {"ticker": "LATE", "entry_date": "2025-11-28", "avg": 100.0, "close": 130.0},  # 57 days: not yet
                     {"ticker": "PAST", "entry_date": "2025-10-01", "avg": 100.0, "close": 130.0},  # already past its anniversary
                     {"ticker": "UNK", "entry_date": "UNKNOWN", "avg": 100.0, "close": 130.0}]
        reports = tax.estimate(self.journal([]), self.RATES, self.NAVS, positions, "2026-10-02")
        near = reports["2026-27"]["nearTwelveMonth"]
        self.assertEqual([(n["ticker"], n["daysToTwelveMonths"]) for n in near], [("NEAR", 54), ("EDGE", 56)])
        self.assertEqual((near[0]["unrealisedGainPct"], near[1]["unrealisedGainPct"]), (0.3, -0.1))


class TradeStatsTests(JournalEnv):
    def build(self, rows, fills=None):
        from app.analyst.ledger import FILL_COLS
        navs = pd.DataFrame({"date": ["2026-05-01"], "nav": [100000.0], "positions_value": [50000.0], "flow": [0.0]})
        fills = fills if fills is not None else pd.DataFrame(columns=FILL_COLS).astype({"qty": "int64", "price": "float64"})
        return evaluator.trade_stats(self.cfg, self.context().cal, FRIDAY, navs, self.journal(rows), fills)

    def test_hit_rate_payoff_ratio_and_expectancy(self):
        s = self.build([("A", "2026-01-01", "2026-02-01", 100.0, 120.0, 100, "FILLS"),   # +1,990
                        ("B", "2026-01-01", "2026-02-01", 100.0, 110.0, 100, "FILLS"),   # +990
                        ("C", "2026-01-01", "2026-02-01", 100.0, 90.0, 100, "FILLS"),    # -1,010
                        ("D", "2026-01-01", "2026-02-01", 100.0, 95.0, 100, "ESTIMATED")])["trades"]  # ignored
        wins, loss = [1990.0, 990.0], 1010.0
        p = 2 / 3
        self.assertEqual((s["count"], round(s["hitRate"], 4), round(s["payoffRatio"], 4)), (3, round(p, 4), round(np.mean(wins) / loss, 4)))
        self.assertAlmostEqual(s["expectancyInr"], p * np.mean(wins) - (1 - p) * loss, places=2)

    def test_turnover_and_cost_drag_over_average_nav(self):
        from app.analyst.ledger import FILL_COLS
        fills = pd.DataFrame([{"fill_key": "a", "trade_date": "2026-02-01", "ticker": "A", "broker_symbol": "A", "side": "BUY", "qty": 100, "price": 200.0,
                               "fill_time": "", "order_id": "", "run_id": "", "kind": "FILL"},
                              {"fill_key": "b", "trade_date": "2026-03-01", "ticker": "A", "broker_symbol": "A", "side": "SELL", "qty": 100, "price": 300.0,
                               "fill_time": "", "order_id": "", "run_id": "", "kind": "FILL"}], columns=FILL_COLS)
        s = self.build([("A", "2026-02-01", "2026-03-01", 200.0, 300.0, 100, "FILLS")], fills)["trades"]
        self.assertAlmostEqual(s["turnover"], (20000 + 30000) / 2 / 100000)  # one-sided
        self.assertAlmostEqual(s["costDrag"], 10 / 100000)

    def test_slippage_beyond_the_stop_by_bucket(self):
        folder = self.risk / "signals"
        folder.mkdir(parents=True)
        (folder / "signals_2026-02-02.json").write_text(json.dumps({"actions": [{"ticker": "A", "bucket": "SmallCap", "reason": "STOP", "detail": {"stopPrice": 100.0}}]}))
        s = self.build([("A", "2026-01-05", "2026-02-03", 130.0, 96.0, 100, "FILLS")])["rules"]["slippageBeyondStop"]
        self.assertAlmostEqual(s["all"]["mean"], 0.04)  # sold at 96 against a stop of 100
        self.assertEqual(s["SmallCap"]["count"], 1)
        self.assertEqual(self.build([("A", "2026-01-05", "2026-02-03", 130.0, 96.0, 100, "ESTIMATED")])["rules"]["slippageBeyondStop"]["all"], {"count": 0})

    def test_whipsaw_rate_counts_stopped_names_reselected_within_4_weeks(self):
        sig = self.risk / "signals"
        sig.mkdir(parents=True)
        for day, t in (("2026-02-02", "A"), ("2026-02-03", "B")):
            (sig / f"signals_{day}.json").write_text(json.dumps({"actions": [{"ticker": t, "bucket": "SmallCap", "reason": "STOP", "detail": {"stopPrice": 1.0}}]}))
        self.targets({"SmallCap": ["A"]}, day="2026-02-20")  # A re-selected 18 days later
        self.targets({"SmallCap": ["B"]}, day="2026-04-10")  # B much later
        self.assertEqual(self.build([])["rules"]["whipsawRate"], 0.5)

    def test_signal_adherence_matches_shadow_signals_to_actual_fills(self):
        from app.analyst.ledger import FILL_COLS
        folder = self.risk / "shadow" / "signals"
        folder.mkdir(parents=True)
        (folder / f"signals_{THURSDAY}.json").write_text(json.dumps({"asOf": THURSDAY, "executionDate": FRIDAY, "actions": [
            {"ticker": "A", "side": "BUY"}, {"ticker": "B", "side": "SELL"}]}))
        (folder / "signals_2026-08-01.json").write_text(json.dumps({"asOf": "2026-08-01", "executionDate": "2026-08-03", "actions": [{"ticker": "OLD", "side": "BUY"}]}))
        fills = pd.DataFrame([{"fill_key": "a", "trade_date": "2026-09-28", "ticker": "A", "broker_symbol": "A", "side": "BUY", "qty": 1, "price": 1.0,
                               "fill_time": "", "order_id": "", "run_id": "", "kind": "FILL"}], columns=FILL_COLS)
        self.assertEqual(self.build([], fills)["rules"]["signalFollowedRate"], 0.5)  # A followed within 3 trading days, B not, OLD out of the week


class PositionRowTests(Env):
    def test_per_symbol_ranks_by_contribution(self):
        rows = pd.DataFrame([{"date": "d1", "ticker": "A", "qty": 1, "close": 1, "value": 1, "day_pl": 100, "day_return": 0.1, "drawdown": 0.0},
                             {"date": "d2", "ticker": "A", "qty": 1, "close": 1, "value": 1, "day_pl": -20, "day_return": -0.02, "drawdown": -0.02},
                             {"date": "d1", "ticker": "B", "qty": 1, "close": 1, "value": 1, "day_pl": 500, "day_return": 0.5, "drawdown": 0.0}]).astype(str)
        out = evaluator.per_symbol(rows)
        self.assertEqual([(o["ticker"], o["plRank"], o["daysHeld"]) for o in out], [("B", 1, 1), ("A", 2, 2)])
        self.assertEqual((out[1]["maxDrawdownFromHwm"], out[1]["plInr"]), (-0.02, 80.0))
        self.assertAlmostEqual(out[1]["returnPct"], 1.1 * 0.98 - 1, places=5)

    def test_positions_rows_value_day_pl_and_drawdown_from_the_high(self):
        self.price("XYZ", closes=[100.0] * 397 + [120.0, 110.0, 108.0])
        held = [{"ticker": "XYZ", "qty": 10, "trackStart": self.days[-3], "avgCostInr": 100.0}]
        from app.risk.common import history
        row, = evaluator.positions_rows(FRIDAY, held, self.store, history)
        self.assertEqual((row["value"], row["day_pl"], row["close"]), (1080.0, -20.0, 108.0))
        self.assertAlmostEqual(row["drawdown"], 108 / 120 - 1, places=5)
        self.assertEqual(evaluator.positions_rows(FRIDAY, [{**held[0], "ticker": "NONE"}], self.store, history), [])


class EvaluateStageTests(JournalEnv):
    def setUp(self):
        super().setUp()
        self.now = datetime(2026, 9, 25, 22, 30, tzinfo=IST)
        self.buckets(SmallCap=["XYZ"])
        self.price("XYZ")
        self.book([("XYZ", 40, 200.0, "2026-01-05")])
        self.surveillance()
        self.targets({"SmallCap": ["XYZ"]})
        self.assertEqual(self.run_risk().status(), "ok")
        self.nav_history()

    def nav_history(self, n=70):
        """Pad the NAV files with earlier days so the statistics have something to chew on."""
        for name in ("actual", "shadow"):
            path = self.risk / "nav" / f"nav_{name}.csv"
            last = pd.read_csv(path)
            days = [d.date().isoformat() for d in pd.bdate_range(end="2026-09-24", periods=n)]
            rng = np.random.default_rng(7)
            twr = np.cumprod(1 + rng.normal(0.001, 0.01, n))
            old = pd.DataFrame({"date": days, "positions_value": 10000.0, "cash": 690000.0, "nav": 700000 * twr, "flow": 0.0, "twr_index": twr, "bench_close": 20000 * np.cumprod(1 + rng.normal(0.0005, 0.01, n)),
                                "active_regime": ["BULL"] * 40 + ["BEAR"] * (n - 40), "rung": 0})
            pd.concat([old, last], ignore_index=True).to_csv(path, index=False)

    def go(self, now=None, force=False):
        report = Report("evaluate")
        evaluate.run(self.cfg, now or self.now, LOG, report, argparse.Namespace(force=force))
        return report

    def test_writes_position_rows_and_friday_statistics_and_tax(self):
        report = self.go()
        self.assertEqual(report.status(), "ok")
        pos = pd.read_csv(self.risk / "nav/positions_daily.csv")
        self.assertEqual((list(pos.date), list(pos.ticker)), ([FRIDAY], ["XYZ"]))
        stats = json.loads((self.risk / "reports/stats_2026-09-25.json").read_text())
        self.assertEqual(set(stats), {"asOf", "benchmark", "benchmarkNote", "riskFreeRatePct", "windows", "perSymbol", "trades", "rules"})
        self.assertIn("inception", stats["windows"])
        self.assertIn("fy:2026-27", stats["windows"])
        self.assertIn("price index", stats["benchmarkNote"])
        self.assertTrue((self.risk / "reports/tax_2026-27.json").exists())
        self.assertEqual(report.block["statsFile"], "reports/stats_2026-09-25.json")

    def test_statistics_only_on_the_stats_day(self):
        self.set_day(THURSDAY)
        self.price("XYZ", days=self.days_to(THURSDAY))
        self.surveillance(day=THURSDAY)
        self.assertEqual(self.run_risk(datetime(2026, 9, 24, 21, 45, tzinfo=IST)).status(), "ok")
        self.nav_history()
        report = self.go(datetime(2026, 9, 24, 22, 30, tzinfo=IST))
        self.assertEqual(report.status(), "ok")
        self.assertFalse((self.risk / "reports").exists())

    def test_rerun_for_the_same_asof_is_a_no_op_and_positions_are_not_duplicated(self):
        self.go()
        common.write_status(self.cfg, self.go(force=True), self.now)
        self.assertEqual(len(pd.read_csv(self.risk / "nav/positions_daily.csv")), 1)
        self.assertTrue(self.go().quiet)

    def test_waits_until_run_has_written_signals(self):
        (self.risk / "signals/signals_2026-09-25.json").unlink()
        with self.assertRaises(Gate):
            self.go()

    def test_final_attempt_without_run_signals_is_a_failed_evaluation(self):
        (self.risk / "signals/signals_2026-09-25.json").unlink()
        report = self.go(datetime(2026, 9, 28, 7, 45, tzinfo=IST))
        self.assertEqual(report.status(), "failed")
        self.assertIn("no NAV rows", report.error)

    def test_a_missing_nav_row_fails_the_evaluation(self):
        path = self.risk / "nav/nav_shadow.csv"
        df = pd.read_csv(path)
        df[df.date != FRIDAY].to_csv(path, index=False)
        report = self.go()
        self.assertEqual(report.status(), "failed")
        self.assertIn("missing in: shadow", report.error)

    def test_evaluate_never_touches_signals_or_state(self):
        before = {p: p.read_bytes() for p in (self.risk / "signals").iterdir()} | {p: p.read_bytes() for p in (self.risk / "state").iterdir()}
        self.go()
        after = {p: p.read_bytes() for p in before}
        self.assertEqual(before, after)

    def test_positions_near_12_months_are_listed_in_the_digest(self):
        self.book([("XYZ", 40, 200.0, "2025-10-10")])  # anniversary 2026-10-10: 15 days after Friday
        (self.risk / "signals/signals_2026-09-25.json").write_text(json.dumps({**json.loads((self.risk / "signals/signals_2026-09-25.json").read_text()),
                                                                              "positions": [{"ticker": "XYZ", "bucket": "SmallCap", "qty": 40, "avgCostInr": 200.0, "closeInr": 250.0, "trackStart": "2025-10-10"}]}))
        report = self.go()
        self.assertTrue(any("Near 12 months: XYZ (15 days, 25%)" in line for line in report.lines))

    def test_tax_estimate_reads_the_journal(self):
        self.analyst.mkdir(exist_ok=True)
        self.journal([("A", "2026-04-10", "2026-05-10", 100.0, 200.0, 1000, "FILLS")]).to_csv(self.analyst / "trading_journal.csv", index=False)
        self.go()
        tax_file = json.loads((self.risk / "reports/tax_2026-27.json").read_text())
        self.assertAlmostEqual(tax_file["netShortTermGainInr"], 99990.0)
        near = json.loads((self.risk / "reports/tax_2026-27.json").read_text())["nearTwelveMonth"]
        self.assertEqual(near, [])


if __name__ == "__main__":
    unittest.main()
