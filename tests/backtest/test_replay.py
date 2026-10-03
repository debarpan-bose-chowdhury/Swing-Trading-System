"""Day loop: decision parity with the app's real commit() path, look-ahead canary, accounting invariants, determinism."""

import copy
import json
import unittest
from pathlib import Path

import pandas as pd

from app.risk import common as risk_common
from backtest import dividends, pit, replay
from backtest.targets import Targets
from tests.backtest.test_targets import World

RISK_CFG = risk_common.load_config("run")  # read before the tests change the working directory
CAPITAL = 700000.0


class Replay(World):
    """The synthetic world of the targets tests plus a risk config whose files live in the temp working directory."""

    def setUp(self):
        super().setUp()
        Path("config/analyst.json").write_text(json.dumps(self.cfg), encoding="utf-8")
        Path("config/indices.json").write_text(json.dumps({"indices": ["^NSEI"]}), encoding="utf-8")
        self.risk = copy.deepcopy(RISK_CFG)
        self.risk["paths"].update(risk="data/risk", analystConfig="config/analyst.json", metadataConfig="config/config.json",
                                  calendar="config/cal.json", indices="config/indices.json")
        risk_common.validate(self.risk, "run")
        self.targets = Targets(self.data, self.cfg)
        self.start, self.end = self.days[400], self.days[560]  # the rising market turns down inside this window

    def sim(self, data=None, targets=None, **kw):
        return replay.simulate(data or self.data, targets or self.targets, self.risk, self.start, kw.pop("end", self.end), CAPITAL, **kw)


class DecisionParity(Replay):
    def test_in_memory_run_equals_the_run_through_the_apps_commit(self):
        fast = self.sim(keep_signals=True)
        slow = self.sim(keep_signals=True, reference_dir=Path("reference"))  # inside the cwd: the app refuses paths outside it
        pd.testing.assert_frame_equal(fast.nav, slow.nav)
        pd.testing.assert_frame_equal(fast.fills, slow.fills)
        strip = lambda sigs: [{k: v for k, v in s.items() if k not in ("generatedAt",)} for s in sigs]  # noqa: E731
        self.assertEqual(strip(fast.signals), strip(slow.signals))
        self.assertGreater(len(fast.fills), 10)  # the comparison is not vacuous
        self.assertGreaterEqual(len({s["ladder"]["rung"] for s in fast.signals}) + len({s["regime"]["active"] for s in fast.signals}), 3)


class Invariants(Replay):
    def test_accounting_holds_every_day(self):
        r = self.sim()
        n = r.nav
        self.assertEqual(len(n), self.days.index(self.end) - self.days.index(self.start) + 1)
        self.assertTrue((n.cash >= 0).all())
        self.assertTrue(((n.positions_value + n.cash - n.nav).abs() <= 0.011).all())
        f = r.fills
        sign = f.side.map({"BUY": -1.0, "SELL": 1.0})
        cash = CAPITAL + (sign * f.qty * f.price).sum() - f.charges.sum()
        self.assertAlmostEqual(cash, n.cash.iloc[-1], delta=1.0)  # only the daily 2-decimal rounding and 4-decimal prices differ
        held = f.assign(q=f.qty * (f.side == "BUY").map({True: 1, False: -1})).groupby("ticker").q.sum()
        self.assertTrue((held >= 0).all())

    def test_runs_are_deterministic(self):
        a, b = self.sim(), self.sim()
        pd.testing.assert_frame_equal(a.nav, b.nav)
        pd.testing.assert_frame_equal(a.fills, b.fills)


class LookAheadCanary(Replay):
    def test_poisoning_data_after_day_t_leaves_everything_up_to_t_unchanged(self):
        t = self.days[480]
        clean = self.sim(end=t, keep_signals=True)
        full = self.sim(end=None, keep_signals=True)
        poisoned = copy.deepcopy(self.data.series)
        for df in poisoned.values():
            late = df.Date > t
            df.loc[late, ["Open", "High", "Low", "Close", "AdjClose"]] *= 37.0
            df.loc[late, "Volume"] = 1
        index = self.data.index.copy()
        index.loc[index.Date > t, "Close"] *= 0.05
        bad_data = pit.PitData(poisoned, index, self.data.buckets)
        hit = self.sim(data=bad_data, targets=Targets(bad_data, self.cfg), end=t, keep_signals=True)
        pd.testing.assert_frame_equal(clean.nav, hit.nav)
        pd.testing.assert_frame_equal(clean.fills, hit.fills)
        self.assertEqual([s["actions"] for s in clean.signals], [s["actions"] for s in hit.signals])
        self.assertEqual(clean.nav.iloc[-1].to_dict(), full.nav[full.nav.date == t].iloc[0].to_dict())  # ending early changes nothing either


class DividendWiring(Replay):
    def test_dividend_on_a_held_name_lifts_that_days_nav_by_exactly_the_credit(self):
        base = self.sim()
        buy = base.fills[base.fills.side == "BUY"].iloc[0]
        i = self.days.index(buy.trade_date)
        ex = next(d for d in self.days[i + 1:i + 4] if not ((base.fills.ticker == buy.ticker) & (base.fills.side == "SELL") & (base.fills.trade_date <= d)).any())
        divs = dividends.Dividends(pd.DataFrame({"Ticker": [buy.ticker], "ExDate": [ex], "Amount": [5.0]}))
        paid = self.sim(dividends=divs)
        qty = paid.dividends[0]["qty"]
        a, b = base.nav.set_index("date").nav, paid.nav.set_index("date").nav
        self.assertEqual(len(paid.dividends), 1)
        self.assertAlmostEqual(b[ex] - a[ex], qty * 5.0, delta=0.011)
        self.assertEqual(list(a[:ex].index[:-1]), list(b[:ex].index[:-1]))
        self.assertTrue(((a[:ex].iloc[:-1] - b[:ex].iloc[:-1]).abs() < 1e-9).all())  # nothing before the ex-date changes


class Seams(Replay):
    def test_patches_are_removed_after_a_run(self):
        from app.risk import nav, run
        load_state, read_nav = run.load_state, nav.read_nav
        self.sim()
        self.assertIs(run.load_state, load_state)
        self.assertIs(nav.read_nav, read_nav)

    def test_bucket_mismatch_is_refused(self):
        self.risk["buckets"] = ["LargeCap"]
        with self.assertRaises(ValueError):
            self.sim()


if __name__ == "__main__":
    unittest.main()
