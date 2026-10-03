"""Stop engine: ATR, ratchet, breach scan, worked example, split safety."""

import unittest

import numpy as np
import pandas as pd

from app.risk import stops

K, LO, HI, PERIOD = 3.5, 0.18, 0.28, 20


def frame(closes, rng=9.0, adj=None, days=None) -> pd.DataFrame:
    days = days or [d.date().isoformat() for d in pd.bdate_range("2025-01-01", periods=len(closes))]
    c = np.array(closes, float)
    return pd.DataFrame({"Date": days, "Open": c, "High": c + rng / 2, "Low": c - rng / 2, "Close": c,
                         "AdjClose": c if adj is None else np.array(adj, float), "Volume": 1000})


class StopTests(unittest.TestCase):
    def test_worked_example_gives_rs_246(self):
        # highest AdjClose 300, ATR20 = 9 -> 3.5 x 9 = 31.5 < 18% x 300 = 54 -> stop = 300 - 54
        closes = [300.0] * 30
        path = stops.stop_path(frame(closes), "2025-01-01", K, LO, HI, PERIOD)
        self.assertAlmostEqual(path.atr.iloc[-1], 9.0)
        self.assertAlmostEqual(path.stop.iloc[-1], 246.0)

    def test_a_close_at_or_below_the_stop_is_a_breach(self):
        df = frame([300.0] * 30 + [245.0])
        path = stops.stop_path(df, "2025-01-01", K, LO, HI, PERIOD)
        b = stops.first_breach(path, None, df.Date.iloc[-1])
        self.assertEqual(b["breachDate"], df.Date.iloc[-1])
        self.assertAlmostEqual(b["stopPrice"], 246.0)
        at = stops.stop_path(frame([300.0] * 30 + [246.0]), "2025-01-01", K, LO, HI, PERIOD)
        self.assertIsNotNone(stops.first_breach(at, None, at.Date.iloc[-1]))  # at the stop counts

    def test_wide_atr_is_clamped_to_the_upper_limit(self):
        path = stops.stop_path(frame([100.0] * 30, rng=40.0), "2025-01-01", K, LO, HI, PERIOD)  # 3.5 x 40 = 140 > 28%
        self.assertAlmostEqual(path.stop.iloc[-1], 100 * (1 - HI))

    def test_ratchet_never_lowers_the_stop(self):
        closes = [100.0] * 25 + list(np.linspace(100, 200, 10)) + [190.0, 180.0, 175.0]
        path = stops.stop_path(frame(closes), "2025-01-01", K, LO, HI, PERIOD)
        self.assertTrue(path.stop.is_monotonic_increasing)
        self.assertAlmostEqual(path.stop.iloc[-1], path.stop.iloc[-4])  # the high-water mark did not move on the way down

    def test_first_bar_of_the_track_can_never_trigger(self):
        df = frame([300.0] * 30 + [100.0])
        path = stops.stop_path(df, df.Date.iloc[-1], K, LO, HI, PERIOD)
        self.assertEqual(len(path), 1)
        self.assertIsNone(stops.first_breach(path, None, df.Date.iloc[-1]))

    def test_scan_covers_every_bar_after_the_last_good_day(self):
        closes = [300.0] * 30 + [240.0, 300.0, 300.0]  # breach on a skipped day, recovered by asOf
        df = frame(closes)
        path = stops.stop_path(df, "2025-01-01", K, LO, HI, PERIOD)
        last_good, asof = df.Date.iloc[29], df.Date.iloc[-1]
        b = stops.first_breach(path, last_good, asof)
        self.assertEqual(b["breachDate"], df.Date.iloc[30])
        self.assertIsNone(stops.first_breach(path, None, asof))  # without a last good day only asOf is checked

    def test_split_does_not_cause_a_false_stop_on_adjclose(self):
        raw = [300.0] * 30 + [150.0] * 5  # 2:1 split: raw Close halves, AdjClose is restated flat
        adj = [150.0] * 35
        path = stops.stop_path(frame(raw, rng=9.0, adj=adj), "2025-01-01", K, LO, HI, PERIOD)
        self.assertIsNone(stops.first_breach(path, "2025-02-01", path.Date.iloc[-1]))

    def test_fewer_than_21_rows_uses_the_widest_clamp(self):
        path = stops.stop_path(frame([100.0] * 20), "2025-01-01", K, LO, HI, PERIOD)
        self.assertTrue(path.atr.isna().all())
        self.assertAlmostEqual(path.stop.iloc[-1], 100 * (1 - HI))
        self.assertFalse(path.atr.iloc[:20].notna().any())
        self.assertTrue(np.isfinite(stops.stop_path(frame([100.0] * 21), "2025-01-01", K, LO, HI, PERIOD).atr.iloc[-1]))

    def test_track_start_before_the_first_row_starts_at_the_first_row(self):
        path = stops.stop_path(frame([100.0] * 30), "1990-01-01", K, LO, HI, PERIOD)
        self.assertEqual(len(path), 30)

    def test_width_pct_nominal_width(self):
        self.assertAlmostEqual(stops.width_pct(9, 250, K, LO, HI), 0.18)  # 3.5 x 3.6% = 12.6% -> lower clamp
        self.assertAlmostEqual(stops.width_pct(20, 250, K, LO, HI), 0.28)  # 28% -> upper clamp
        self.assertAlmostEqual(stops.width_pct(18, 250, K, LO, HI), 0.252)
        self.assertEqual(stops.width_pct(float("nan"), 250, K, LO, HI), HI)


if __name__ == "__main__":
    unittest.main()
