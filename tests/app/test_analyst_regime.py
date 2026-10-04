"""Regime classification, weekly rebalance dates and persistence."""

import random
import unittest
from datetime import date

import pandas as pd

from app.analyst import regime
from app.market.tradingcal import Calendar


def series(values, start="2020-01-01"):
    return pd.Series([float(v) for v in values], index=pd.date_range(start, periods=len(values), freq="B"))


def reference_state(raw, persistence):
    """The strategy repo's build_strategy_plan persistence loop (initial regime Unknown instead of BEAR)."""
    active = pending = "Unknown"
    count, out = 0, []
    for r in raw:
        if r == active:
            pending, count = active, 0
        elif r == pending:
            count += 1
            if count >= persistence:
                active, count = pending, 0
        else:
            pending, count = r, 1
        if r == "BEAR" and active != "BEAR":
            active, pending, count = "BEAR", "BEAR", 0
        out.append((r, active, pending))
    return out


class RawRegimeTests(unittest.TestCase):
    def last(self, values):
        return regime.raw_regimes(series(values)).iloc[-1]

    def test_four_regimes(self):
        self.assertEqual(self.last([1000 + 2 * i for i in range(250)]), "BULL")
        self.assertEqual(self.last([1000 - 2 * i for i in range(250)]), "BEAR")
        self.assertEqual(self.last([1000] * 200 + [900] * 40 + [950] * 10), "WEAK")
        self.assertEqual(self.last([1000 + i for i in range(200)] + [1200] * 40 + [1190] * 10), "TREND")

    def test_above_both_averages_but_falling_over_63_days_is_trend(self):
        # Close above SMA200 and SMA50 yet below its level 63 days ago
        values = [1000] * 150 + [2000] * 20 + [1800] * 40 + [1900] * 10
        self.assertEqual(self.last(values), "TREND")

    def test_first_209_observations_are_unknown(self):
        raw = regime.raw_regimes(series([1000 + i for i in range(260)]))
        self.assertTrue((raw.iloc[:209] == "Unknown").all())
        self.assertNotEqual(raw.iloc[209], "Unknown")

    def test_default_windows_equal_explicit_50_200_63(self):
        close = series([1000 + 3 * i + (i % 7) * 5 for i in range(400)])
        pd.testing.assert_series_equal(regime.raw_regimes(close), regime.raw_regimes(close, (50, 200, 63)))

    def test_slower_window_extends_the_unknown_period(self):
        raw = regime.raw_regimes(series([1000 + i for i in range(400)]), (50, 300, 63))
        self.assertTrue((raw.iloc[:309] == "Unknown").all())
        self.assertNotEqual(raw.iloc[309], "Unknown")

    def test_windows_come_from_config_with_defaults(self):
        self.assertEqual(regime.windows_of({"regime": {}}), (50, 200, 63))
        self.assertEqual(regime.windows_of({"regime": {"smaFast": 20, "smaSlow": 100, "momentumDays": 40}}), (20, 100, 40))

    def test_equal_to_the_average_counts_as_not_above(self):
        self.assertEqual(self.last([1000] * 250), "BEAR")


class RebalanceDateTests(unittest.TestCase):
    def test_last_weekday_row_of_each_iso_week(self):
        idx = pd.DatetimeIndex(["2026-09-28", "2026-09-29", "2026-10-01", "2026-10-05", "2026-10-06"])  # Fri 10-02 holiday
        got = [d.date().isoformat() for d in regime.rebalance_dates(idx)]
        self.assertEqual(got, ["2026-10-01", "2026-10-06"])

    def test_weekend_sessions_are_ignored(self):
        idx = pd.DatetimeIndex(["2026-11-02", "2026-11-06", "2026-11-08"])  # Sunday Muhurat session
        self.assertEqual([d.date().isoformat() for d in regime.rebalance_dates(idx)], ["2026-11-06"])

    def test_year_boundary_week_is_one_week(self):
        idx = pd.DatetimeIndex(["2025-12-29", "2025-12-31", "2026-01-01", "2026-01-02"])  # ISO week 1 of 2026
        self.assertEqual([d.date().isoformat() for d in regime.rebalance_dates(idx)], ["2026-01-02"])

    def test_live_date_uses_the_calendar_and_the_whole_week_on_a_weekend(self):
        cal = Calendar.__new__(Calendar)
        cal.holidays, cal.special = {"2026-10-02"}, {"2026-10-03"}  # holiday Friday, special Saturday session
        for today in (date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 4)):
            self.assertEqual(regime.live_rebalance_date(cal, today), date(2026, 10, 1))
        cal.holidays = {f"2026-10-0{d}" for d in range(5, 10)}
        self.assertIsNone(regime.live_rebalance_date(cal, date(2026, 10, 7)))


class PersistenceTests(unittest.TestCase):
    def state(self, regimes, persistence=2):
        raw = pd.Series(regimes, index=pd.date_range("2026-01-02", periods=len(regimes), freq="W-FRI"))
        return regime.weekly_state(raw, persistence)

    def test_non_bear_regimes_activate_on_the_nth_consecutive_week(self):
        df = self.state(["BULL", "BULL", "TREND", "BULL", "BULL"], persistence=2)
        self.assertEqual(list(df.active_regime), ["Unknown", "BULL", "BULL", "BULL", "BULL"])
        self.assertEqual(list(df.pending_regime), ["BULL", "BULL", "TREND", "BULL", "BULL"])

    def test_bear_activates_at_once_and_leaving_it_takes_persistence_weeks(self):
        df = self.state(["BULL", "BULL", "BEAR", "BULL", "BULL"], persistence=2)
        self.assertEqual(list(df.active_regime), ["Unknown", "BULL", "BEAR", "BEAR", "BULL"])

    def test_remaining_days_count_down_and_are_zero_when_nothing_is_pending(self):
        df = self.state(["BULL", "BULL", "BULL", "TREND", "TREND", "TREND", "TREND"], persistence=4)
        self.assertEqual(list(df.pending_remaining_days), [21, 14, 7, 21, 14, 7, 0])
        df = self.state(["BULL", "BULL", "TREND"], persistence=2)
        self.assertEqual(list(df.pending_remaining_days), [7, 0, 7])

    def test_persistence_one_follows_the_raw_regime(self):
        df = self.state(["BULL", "TREND", "WEAK"], persistence=1)
        self.assertEqual(list(df.active_regime), ["BULL", "TREND", "WEAK"])

    def test_matches_the_strategy_repo_loop_on_random_sequences(self):
        rng = random.Random(7)
        for persistence in (2, 3, 4):  # the repo loop never activates on a first occurrence, so its 1 behaves like 2
            for _ in range(200):
                raw = [rng.choice(["Unknown", "BULL", "TREND", "WEAK", "BEAR"]) for _ in range(rng.randint(1, 40))]
                got = self.state(raw, persistence)
                want = reference_state(raw, persistence)
                self.assertEqual(list(zip(got.raw_regime, got.active_regime, got.pending_regime)), want, (persistence, raw))


class HistoryTests(unittest.TestCase):
    def test_history_is_stateless_and_replays_identically(self):
        close = series([1000 + (i % 40) * 3 + i for i in range(400)])
        full = regime.regime_history(close, 2)
        cut = regime.regime_history(close[:298], 2)  # position 297 is a Friday, so the last week is complete
        self.assertEqual(full.iloc[: len(cut)].to_dict("records"), cut.to_dict("records"))
        self.assertEqual(list(full.columns), regime.HISTORY_COLS)


if __name__ == "__main__":
    unittest.main()


class ExposedRegimeNumbers(unittest.TestCase):
    def test_h5_momentum_threshold_gates_bull(self):
        close = series([1000 + 2 * i for i in range(250)])
        self.assertEqual(regime.raw_regimes(close).iloc[-1], "BULL")
        self.assertEqual(regime.raw_regimes(close, momentum_threshold=0.0).iloc[-1], "BULL")
        self.assertEqual(regime.raw_regimes(close, momentum_threshold=5.0).iloc[-1], "TREND")  # above both averages, but 63-day return < 500%

    def test_h6_unknown_extra_sets_the_warm_up(self):
        close = series([1000 + i for i in range(260)])
        pd.testing.assert_series_equal(regime.raw_regimes(close), regime.raw_regimes(close, unknown_extra=9))
        raw = regime.raw_regimes(close, unknown_extra=0)
        self.assertTrue((raw.iloc[:200] == "Unknown").all())
        self.assertNotEqual(raw.iloc[200], "Unknown")

    def test_shape_comes_from_config_with_defaults(self):
        self.assertEqual(regime.shape_of({"regime": {}}), {"momentum_threshold": 0.0, "unknown_extra": 9})
        self.assertEqual(regime.shape_of({"regime": {"momentumThreshold": 0.1, "unknownExtra": 3}}), {"momentum_threshold": 0.1, "unknown_extra": 3})

    def test_history_passes_the_shape_through(self):
        close = series([1000 + 2 * i for i in range(300)])
        self.assertNotEqual(list(regime.regime_history(close, 1).raw_regime), list(regime.regime_history(close, 1, momentum_threshold=5.0).raw_regime))
