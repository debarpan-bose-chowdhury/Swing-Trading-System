"""Exposure ladder: rounding, immediate step-down, weekly step-up, flat lock, manual restart, regime and ladder caps."""

import json
import unittest
from pathlib import Path

from app.risk import ladder


def cfg(restart=None):
    c = json.loads(Path("app/config/risk.json").read_text())
    c["ladder"]["restartFrom"] = restart
    return c


def go(state, twr, asof, c, rebalance=False, regimes=("BULL", "BULL"), hist=None):
    return ladder.step(state, twr, asof, c, rebalance, list(regimes), hist if hist is not None else [1.0] * 25)


class LadderTests(unittest.TestCase):
    def setUp(self):
        self.c = cfg()
        self.s = ladder.new_state("2026-09-01", 1.0)

    def test_new_state_matches_the_tdd_fields(self):
        self.assertEqual(set(self.s), {"rung", "peakIndex", "peakDate", "baselineIndex", "flatLocked", "flatLockedSince",
                                       "lastRestartFrom", "lastReRiskWeek", "shadowStartDate"})

    def test_float_noise_is_rounded_to_six_decimals_before_comparing(self):
        _, info = go(self.s, 1 - 0.14999999999999997, "2026-09-02", self.c)  # exactly the 15% level once rounded
        self.assertEqual(info["rung"], 2)
        _, info = go(self.s, 1 - 0.09999999999999997, "2026-09-02", self.c)
        self.assertEqual(info["rung"], 1)
        _, info = go(self.s, 0.900001, "2026-09-02", self.c)  # 9.9999%: below 10%
        self.assertEqual(info["rung"], 0)

    def test_step_down_is_immediate_and_skips_rungs(self):
        _, info = go(self.s, 0.78, "2026-09-02", self.c)  # 22% drawdown -> rung 3
        self.assertEqual((info["rung"], info["maxInvestedPct"]), (3, 0.25))

    def test_peak_follows_new_highs(self):
        s, _ = go(self.s, 1.2, "2026-09-02", self.c)
        self.assertEqual((s["peakIndex"], s["peakDate"]), (1.2, "2026-09-02"))
        _, info = go(s, 1.2 * 0.9, "2026-09-03", self.c)
        self.assertEqual(info["rung"], 1)

    def test_step_up_one_rung_per_week_and_only_on_rebalance(self):
        s, _ = go(self.s, 0.78, "2026-09-02", self.c)  # rung 3
        hist = [0.7] * 25
        s, info = go(s, 0.95, "2026-09-04", self.c, rebalance=False, hist=hist)
        self.assertEqual(info["rung"], 3)  # not a rebalance day
        s, info = go(s, 0.95, "2026-09-04", self.c, rebalance=True, hist=hist)
        self.assertEqual((info["rung"], s["lastReRiskWeek"]), (2, "2026-W36"))
        s, info = go(s, 0.97, "2026-09-04", self.c, rebalance=True, hist=hist)
        self.assertEqual(info["rung"], 2)  # a rerun in the same ISO week cannot step twice
        s, info = go(s, 0.97, "2026-09-11", self.c, rebalance=True, hist=hist)
        self.assertEqual(info["rung"], 1)

    def test_step_up_needs_regime_and_nav_conditions(self):
        s, _ = go(self.s, 0.78, "2026-09-02", self.c)
        hist = [0.7] * 25
        _, info = go(s, 0.95, "2026-09-04", self.c, rebalance=True, regimes=("BULL", "WEAK"), hist=hist)
        self.assertEqual((info["rung"], info["reRiskEligible"]), (3, False))
        _, info = go(s, 0.95, "2026-09-04", self.c, rebalance=True, regimes=("BULL",), hist=hist)
        self.assertEqual(info["rung"], 3)  # fewer than 2 weekly rows
        _, info = go(s, 0.95, "2026-09-04", self.c, rebalance=True, hist=[1.0] * 25)
        self.assertEqual(info["rung"], 3)  # index not above its previous 20-day minimum
        _, info = go(s, 0.95, "2026-09-04", self.c, rebalance=True, hist=[0.7] * 5)
        self.assertEqual(info["rung"], 3)  # less than 20 days of history

    def test_no_step_up_while_drawdown_still_demands_the_rung(self):
        s, _ = go(self.s, 0.88, "2026-09-02", self.c)  # rung 1, floor 1
        _, info = go(s, 0.88, "2026-09-04", self.c, rebalance=True, hist=[0.7] * 25)
        self.assertEqual(info["rung"], 1)

    def test_rung_4_locks_flat_whatever_the_drawdown_does(self):
        s, info = go(self.s, 0.74, "2026-09-02", self.c)
        self.assertEqual((info["rung"], s["flatLocked"], s["flatLockedSince"], info["maxInvestedPct"]), (4, True, "2026-09-02", 0.0))
        s, info = go(s, 1.0, "2026-09-11", self.c, rebalance=True, hist=[0.7] * 25)
        self.assertEqual((info["rung"], info["flatLocked"]), (4, True))

    def test_restart_resets_peak_and_starts_at_rung_3(self):
        s, _ = go(self.s, 0.74, "2026-09-02", self.c)
        c = cfg(restart="2026-09-10")
        s, info = go(s, 0.80, "2026-09-10", c)
        self.assertEqual((info["rung"], s["flatLocked"], s["peakIndex"], s["baselineIndex"], s["lastRestartFrom"]), (3, False, 0.80, 0.80, "2026-09-10"))
        self.assertEqual(info["drawdownPct"], 0.0)
        s, info = go(s, 0.80, "2026-09-11", c)  # the same restartFrom does not fire again
        self.assertEqual(info["rung"], 3)

    def test_restart_in_the_future_or_before_the_lock_is_ignored(self):
        s, _ = go(self.s, 0.74, "2026-09-02", self.c)
        _, info = go(s, 0.80, "2026-09-10", cfg(restart="2026-09-20"))
        self.assertEqual(info["rung"], 4)
        _, info = go(s, 0.80, "2026-09-10", cfg(restart="2026-09-01"))
        self.assertEqual(info["rung"], 4)  # not after flatLockedSince

    def test_restart_without_a_lock_does_nothing(self):
        s, info = go(self.s, 1.0, "2026-09-10", cfg(restart="2026-09-05"))
        self.assertEqual((info["rung"], s["lastRestartFrom"]), (0, None))

    def test_caps_take_the_lower_and_ties_go_to_the_ladder(self):
        c = cfg()
        self.assertEqual(ladder.caps(c, "BULL", 0), {"regimeCap": 1.0, "ladderCap": 1.0, "finalCap": 1.0, "reason": "LADDER"})
        c["exposure"]["regimeCap"]["BEAR"] = 0.5
        self.assertEqual(ladder.caps(c, "BEAR", 1)["reason"], "REGIME_CAP")  # 50% < 75%
        self.assertEqual(ladder.caps(c, "BEAR", 2)["reason"], "LADDER")  # tie at 50%
        self.assertEqual(ladder.caps(c, "BEAR", 3)["finalCap"], 0.25)
        self.assertEqual(ladder.caps(c, "Unknown", 0)["finalCap"], 1.0)


if __name__ == "__main__":
    unittest.main()


class ExposedLadderNumbers(unittest.TestCase):
    def setUp(self):
        self.c = cfg()
        self.s = ladder.new_state("2026-09-01", 1.0)

    def test_h8_rungs_per_week_default_is_one(self):
        self.c["ladder"]["reRisk"]["rungsPerWeek"] = 1
        s, _ = go(self.s, 0.78, "2026-09-02", self.c)
        _, info = go(s, 0.95, "2026-09-04", self.c, rebalance=True, hist=[0.7] * 25)
        self.assertEqual(info["rung"], 2)

    def test_h8_two_rungs_per_week_and_never_below_the_drawdown_floor(self):
        self.c["ladder"]["reRisk"]["rungsPerWeek"] = 2
        s, _ = go(self.s, 0.78, "2026-09-02", self.c)  # rung 3
        s, info = go(s, 0.95, "2026-09-04", self.c, rebalance=True, hist=[0.7] * 25)  # drawdown 5%: floor 0
        self.assertEqual(info["rung"], 1)
        s, info = go(s, 0.95, "2026-09-04", self.c, rebalance=True, hist=[0.7] * 25)  # the same ISO week: no second step
        self.assertEqual(info["rung"], 1)
        s, info = go(s, 0.95, "2026-09-11", self.c, rebalance=True, hist=[0.7] * 25)
        self.assertEqual(info["rung"], 0)
        s2, _ = go(self.s, 0.78, "2026-09-02", self.c)
        _, info = go(s2, 0.88, "2026-09-04", self.c, rebalance=True, hist=[0.7] * 25)  # 12% drawdown: floor 1, so 3 - 2 = 1
        self.assertEqual(info["rung"], 1)
        s3, _ = go(self.s, 0.78, "2026-09-02", self.c)
        _, info = go(s3, 0.84, "2026-09-04", self.c, rebalance=True, hist=[0.7] * 25)  # 16% drawdown: floor 2 stops the step at 2
        self.assertEqual(info["rung"], 2)

    def test_h8_restart_rung_offset(self):
        s, _ = go(self.s, 0.74, "2026-09-02", self.c)
        for offset, rung in ((None, 3), (1, 3), (2, 2), (4, 0)):
            c = cfg(restart="2026-09-10")
            if offset:
                c["ladder"]["restartRungOffset"] = offset
            _, info = go(s, 0.80, "2026-09-10", c)
            self.assertEqual(info["rung"], rung, offset)
