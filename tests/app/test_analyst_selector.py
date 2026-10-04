"""Selector: exclusions, ranking, momentum skip, BEAR composite score, panel loading, bucket universe."""

import copy
import os
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from app.analyst import common, selector
from app.market.store import Store
from tests.app.market_helpers import bars, weekdays

CFG = common.load_config()  # read before any test changes the working directory
RD = "2026-10-02"
STRATEGY = {"top_n": 3, "lookback": 20, "stock_trend_ma": 50}


def sel(**over) -> dict:
    s = copy.deepcopy(CFG["selector"])
    s["liquidity"].update(windowDays=5, minAdvCr=1)
    s.update(over)
    return s


def rising(n=120, start=100.0, step=1.0):
    return [start + step * i for i in range(n)]


def panel(prices: dict, volume=1e7):
    n = max(len(v) for v in prices.values())
    idx = pd.bdate_range(end=RD, periods=n)
    adj = pd.DataFrame({k: pd.Series(v, index=idx[-len(v):], dtype=float) for k, v in prices.items()}).reindex(idx)
    vol = volume if isinstance(volume, dict) else {k: volume for k in prices}
    value = pd.DataFrame({k: adj[k] * vol[k] for k in prices})
    return adj, value


def pick(prices, strategy=None, regime="BULL", s=None, volume=1e7):
    adj, value = panel(prices, volume)
    return selector.select_bucket(adj, value, RD, strategy or STRATEGY, regime, s or sel())


def names(picks):
    return [p["ticker"] for p in picks]


class SelectionTests(unittest.TestCase):
    def test_ranks_by_momentum_and_cuts_to_top_n(self):
        picks, counts = pick({"A": rising(step=1), "B": rising(step=3), "C": rising(step=2), "D": rising(step=0.5)})
        self.assertEqual(names(picks), ["B", "C", "A"])
        self.assertEqual([p["rank"] for p in picks], [1, 2, 3])
        self.assertEqual(counts, {"noRowOnRebalanceDate": 0, "insufficientHistory": 0, "illiquid": 0})

    def test_momentum_matches_the_original_formula(self):
        picks, _ = pick({"A": rising()})
        p = rising()
        self.assertAlmostEqual(picks[0]["momentum"], round(p[-1] / p[-21] - 1, 6))
        self.assertAlmostEqual(picks[0]["trendMa"], round(float(np.mean(p[-50:])), 6))
        self.assertEqual(picks[0]["price"], p[-1])

    def test_ties_break_on_symbol_ascending(self):
        picks, _ = pick({"Z": rising(), "A": rising(), "M": rising()}, {**STRATEGY, "top_n": 2})
        self.assertEqual(names(picks), ["A", "M"])

    def test_negative_momentum_and_price_below_trend_are_not_candidates(self):
        falling = [300 - i for i in range(120)]
        below_ma = [100.0] * 70 + [200.0] * 25 + [90 + i * 0.4 for i in range(25)]  # momentum > 0 but under its 50-day mean
        picks, counts = pick({"UP": rising(), "DOWN": falling, "LOW": below_ma})
        self.assertEqual(names(picks), ["UP"])
        self.assertEqual(sum(counts.values()), 0)

    def test_no_row_on_the_rebalance_date_is_excluded_not_filled(self):
        stale = rising()
        stale[-1] = np.nan
        picks, counts = pick({"A": rising(), "STALE": stale})
        self.assertEqual(names(picks), ["A"])
        self.assertEqual(counts["noRowOnRebalanceDate"], 1)

    def test_gap_inside_the_trend_window_excludes_the_ticker(self):
        gap = rising()
        gap[-30] = np.nan
        picks, counts = pick({"A": rising(), "GAP": gap})
        self.assertEqual(names(picks), ["A"])
        self.assertEqual(counts["insufficientHistory"], 1)

    def test_short_history_ticker_is_excluded(self):
        picks, counts = pick({"A": rising(), "NEW": rising(n=40)})
        self.assertEqual(names(picks), ["A"])
        self.assertEqual(counts["insufficientHistory"], 1)

    def test_bucket_history_gate_gives_no_selection(self):
        picks, counts = pick({"A": rising(n=52), "B": rising(n=52)})
        self.assertEqual((picks, counts["insufficientHistory"]), ([], 2))

    def test_top_n_zero_and_empty_panel_select_nothing(self):
        self.assertEqual(pick({"A": rising()}, {**STRATEGY, "top_n": 0})[0], [])
        picks, _ = selector.select_bucket(pd.DataFrame(), pd.DataFrame(), RD, STRATEGY, "BULL", sel())
        self.assertEqual(picks, [])

    def test_liquidity_uses_the_median_so_one_spike_does_not_pass_a_thin_stock(self):
        adj, value = panel({"THIN": rising(), "DEEP": rising()}, volume=1e7)
        value["THIN"] = adj["THIN"] * 100  # about 0.002 crore a day
        value.iloc[-1, value.columns.get_loc("THIN")] = 1e10  # one block-deal day
        picks, counts = selector.select_bucket(adj, value, RD, STRATEGY, "BULL", sel())
        self.assertEqual((names(picks), counts["illiquid"]), (["DEEP"], 1))

    def test_liquidity_needs_the_full_window(self):
        adj, value = panel({"A": rising(), "B": rising()})
        value.iloc[-3, value.columns.get_loc("B")] = np.nan
        picks, counts = selector.select_bucket(adj, value, RD, STRATEGY, "BULL", sel())
        self.assertEqual((names(picks), counts["insufficientHistory"]), (["A"], 1))

    def test_momentum_skip_drops_the_most_recent_days(self):
        p = rising() [:-10] + [rising()[-11]] * 10  # flat for the last 10 days
        base, _ = pick({"A": p})
        skipped, _ = pick({"A": p}, s=sel(momentumSkipDays=10))
        self.assertAlmostEqual(base[0]["momentum"], round(p[-1] / p[-21] - 1, 6))
        self.assertAlmostEqual(skipped[0]["momentum"], round(p[-11] / p[-31] - 1, 6))


def reference_order(series: pd.Series) -> tuple[int, float]:
    """The strategy repo's _score_symbol formula for one ticker: (tier, -score), lower sorts first."""
    px = series.dropna()
    mom20, mom63 = px.iloc[-1] / px.iloc[-21] - 1, px.iloc[-1] / px.iloc[-64] - 1
    ret = px.pct_change().dropna().iloc[-20:]
    dd63 = px.iloc[-1] / px.iloc[-64:].max() - 1
    score = 0.40 * mom20 + 0.35 * mom63 + 0.20 * (ret > 0).mean() - 0.35 * ret.std(ddof=0) + 0.10 * dd63
    return (0 if mom20 > 0 and mom63 > 0 else 1, -score)


class BearTests(unittest.TestCase):
    def test_pool_is_not_cut_and_order_follows_the_repo_composite_score(self):
        rng = np.random.default_rng(3)
        prices = {f"S{i}": list(100 * np.cumprod(1 + rng.normal(0.004, 0.005 + 0.004 * i, 160))) for i in range(8)}
        everyone, _ = pick(prices, {**STRATEGY, "top_n": 99}, regime="BULL")
        bear, _ = pick(prices, {**STRATEGY, "top_n": 99}, regime="BEAR")
        self.assertGreaterEqual(len(everyone), 4)
        self.assertEqual(set(names(bear)), set(names(everyone)))
        adj, _ = panel(prices)
        want = sorted(names(everyone), key=lambda t: (*reference_order(adj[t]), t))
        self.assertEqual(names(bear), want)
        self.assertNotEqual(names(bear), names(everyone))  # the score really reorders
        cut, _ = pick(prices, {**STRATEGY, "top_n": 3}, regime="BEAR")
        self.assertEqual(names(cut), want[:3])  # top_n is applied after the score, not before

    def test_momentum_confirmed_names_rank_ahead_of_higher_momentum_backup_names(self):
        pullback = list(np.linspace(100, 300, 70)) + list(np.linspace(300, 200, 28)) + list(np.linspace(200, 230, 22))
        steady = list(np.linspace(150, 180, 120))
        strategy = {**STRATEGY, "stock_trend_ma": 20}
        plain, _ = pick({"B": pullback, "A": steady}, strategy, regime="BULL")
        bear, _ = pick({"B": pullback, "A": steady}, strategy, regime="BEAR")
        self.assertEqual(names(plain), ["B", "A"])  # B has the stronger 20-day momentum
        self.assertEqual(names(bear), ["A", "B"])  # but A alone has positive 20- and 63-day momentum; B only fills the slot

    def test_only_qualifiers_are_ever_picked_in_bear(self):
        picks, _ = pick({"UP": rising(), "DOWN": [300 - i for i in range(120)]}, regime="BEAR")
        self.assertEqual(names(picks), ["UP"])

    def test_candidates_without_70_full_rows_are_dropped(self):
        short = rising(100)
        short[-65:-60] = [np.nan] * 5
        adj, value = panel({"A": rising(), "S": short})
        strategy = {**STRATEGY, "stock_trend_ma": 20}
        picks, counts = selector.select_bucket(adj, value, RD, strategy, "BEAR", sel())
        self.assertEqual(names(picks), ["A"])
        self.assertEqual(counts["insufficientHistory"], 1)


class LoadingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        previous = os.getcwd()
        os.chdir(self.root)  # store paths must stay inside the working directory, like /app in the container
        self.addCleanup(os.chdir, previous)

    def test_rows_needed_covers_every_window(self):
        cfg = copy.deepcopy(CFG)
        self.assertEqual(selector.rows_needed(cfg), 168 + 2 + 5)  # lookback + 2 beats trend 150 + 5, the 70-row score and the 20-day liquidity window
        cfg["selector"]["momentumSkipDays"] = 21
        self.assertEqual(selector.rows_needed(cfg), 168 + 21 + 2 + 5)

    def test_panel_stops_at_the_rebalance_date_and_never_fills(self):
        store = Store("market", cutoff="2026-01-01")
        days = weekdays("2026-08-03", "2026-10-09")
        store.upsert("A", bars("A", days))
        store.upsert("B", bars("B", [d for d in days if d != RD]))
        adj, value, missing = selector.load_panel(store, ["A", "B", "NONE"], RD, 30)
        self.assertEqual(adj.index.max(), pd.Timestamp(RD))
        self.assertEqual(len(adj), 31)  # A's 30 rows plus the one earlier day B's 30 rows reach
        self.assertTrue(np.isnan(adj.loc[RD, "B"]))
        self.assertEqual(missing, 1)
        self.assertEqual(value.loc[RD, "A"], 100.0 * 1000)

    def test_archive_is_read_only_when_the_fresh_tier_is_too_short(self):
        store = Store("market", cutoff="2026-09-01")
        store.rebuild("A", bars("A", weekdays("2026-06-01", "2026-10-02")))  # old part goes to parquet
        short, _, _ = selector.load_panel(store, ["A"], RD, 10)
        long, _, _ = selector.load_panel(store, ["A"], RD, 80)
        self.assertEqual((len(short), len(long)), (10, 80))
        self.assertTrue(long.index.is_monotonic_increasing and long.index.is_unique)

    def test_bucket_universe_picks_newest_file_on_or_before_the_date_and_rejects_stale_ones(self):
        cfg = copy.deepcopy(CFG)
        storage = self.root / "storage"
        storage.mkdir()
        cfg["paths"]["upstreamStorage"] = str(storage)
        for day, syms in (("2026-09-29", "Symbol\nOLD\n"), ("2026-10-01", "Symbol\nB\nA\n"), ("2026-10-05", "Symbol\nFUTURE\n")):
            (storage / f"SmallCap_{day}.csv").write_text(syms, encoding="utf-8")
        (storage / "MidCap_2026-10-01.csv").write_text("Symbol\nNOPE\n", encoding="utf-8")
        self.assertEqual(selector.bucket_universe(cfg, "SmallCap", RD), ("2026-10-01", ["A", "B"]))
        with self.assertRaises(ValueError):
            selector.bucket_universe(cfg, "SmallCap", "2026-10-30")  # newest file is 29 days old
        with self.assertRaises(ValueError):
            selector.bucket_universe(cfg, "LargeCap", RD)


if __name__ == "__main__":
    unittest.main()


def bear_prices():
    rng = np.random.default_rng(3)
    return {f"S{i}": list(100 * np.cumprod(1 + rng.normal(0.004, 0.005 + 0.004 * i, 160))) for i in range(8)}


class ExposedSelectorNumbers(unittest.TestCase):
    """Parameter Exposure H1-H4: each number set to its default changes nothing; set to another value it moves the result."""

    def bear(self, **over):
        s = sel()
        s["bearScore"] = {**s["bearScore"], **over}
        return pick(bear_prices(), {**STRATEGY, "top_n": 99}, regime="BEAR", s=s)[0]

    def test_h1_default_windows_equal_the_hardcoded_ones(self):
        explicit = self.bear(windows={"shortDays": 20, "longDays": 63, "hitDays": 20, "volDays": 20, "ddDays": 63}, confirmThreshold=0.0)
        self.assertEqual(explicit, self.bear())
        self.assertEqual(selector.bear_rows(CFG["selector"]["bearScore"]), 70)
        self.assertEqual(selector.rows_needed(CFG), selector.rows_needed({**CFG, "selector": {**CFG["selector"], "bearScore": {k: v for k, v in CFG["selector"]["bearScore"].items() if k != "windows"}}}))

    def test_h1_each_window_changes_the_score(self):
        base = {p["ticker"]: p["score"] for p in self.bear()}
        for key, value in (("shortDays", 10), ("longDays", 40), ("hitDays", 10), ("volDays", 40), ("ddDays", 5)):
            moved = {p["ticker"]: p["score"] for p in self.bear(windows={key: value})}
            self.assertNotEqual(moved, base, key)

    def test_h1_score_follows_the_configured_windows(self):
        adj, _ = panel(bear_prices())
        w = {"shortDays": 10, "longDays": 40, "hitDays": 15, "volDays": 30, "ddDays": 25}
        got = {p["ticker"]: p["score"] for p in self.bear(windows=w)}
        for t, score in got.items():
            px = adj[t].dropna()
            ret = px.pct_change()
            want = (0.40 * (px.iloc[-1] / px.iloc[-11] - 1) + 0.35 * (px.iloc[-1] / px.iloc[-41] - 1) + 0.20 * (ret.iloc[-15:] > 0).mean()
                    - 0.35 * ret.iloc[-30:].std(ddof=0) + 0.10 * (px.iloc[-1] / px.iloc[-26:].max() - 1))
            self.assertAlmostEqual(score, round(want, 6), places=6)

    def test_h1_minimum_rows_follow_the_longest_window(self):
        self.assertEqual(selector.bear_rows({"windows": {"longDays": 100}}), 107)
        need = lambda longest: selector.rows_needed({**CFG, "selector": {**CFG["selector"], "bearScore": {**CFG["selector"]["bearScore"], "windows": {"longDays": longest}}}})  # noqa: E731
        self.assertGreater(need(300), need(63))

    def test_h2_min_momentum_filters_candidates_in_every_regime(self):
        prices = {"A": rising(step=1), "B": rising(step=3)}  # 20-day momentum: A about 0.10, B about 0.15
        self.assertEqual(names(pick(prices)[0]), ["B", "A"])
        self.assertEqual(names(pick(prices, s=sel(minMomentum=0.0))[0]), ["B", "A"])
        self.assertEqual(names(pick(prices, s=sel(minMomentum=0.12))[0]), ["B"])
        self.assertEqual(names(pick(prices, regime="BEAR", s=sel(minMomentum=0.12))[0]), ["B"])

    def test_h3_trend_buffer_requires_price_above_the_average_by_the_margin(self):
        prices = {"A": rising()}
        picks, _ = pick(prices)
        gap = picks[0]["price"] / picks[0]["trendMa"] - 1
        self.assertEqual(names(pick(prices, s=sel(trendBuffer=0.0))[0]), ["A"])
        self.assertEqual(names(pick(prices, s=sel(trendBuffer=gap * 0.9))[0]), ["A"])
        self.assertEqual(names(pick(prices, s=sel(trendBuffer=gap * 1.1))[0]), [])

    def test_h4_confirm_threshold_moves_the_bear_tiers(self):
        pullback = list(np.linspace(100, 300, 70)) + list(np.linspace(300, 200, 28)) + list(np.linspace(200, 230, 22))
        steady = list(np.linspace(150, 180, 120))
        strategy = {**STRATEGY, "stock_trend_ma": 20}
        invert = {"mom20": -1.0, "mom63": -1.0, "hit20": 0.0, "vol20": 0.0, "dd63": 0.0}  # B scores higher than A, but only A has positive momentum
        tiered = lambda **o: names(pick({"B": pullback, "A": steady}, strategy, regime="BEAR", s=sel(bearScore={**invert, **o}))[0])  # noqa: E731
        self.assertEqual(tiered(), ["A", "B"])
        self.assertEqual(tiered(confirmThreshold=0.0), ["A", "B"])
        self.assertEqual(tiered(confirmThreshold=-0.9), ["B", "A"])  # both confirm now: the score alone orders them
        self.assertEqual(tiered(confirmThreshold=5.0), ["B", "A"])  # neither confirms: the same
