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
from tests import fixtures

with fixtures.pinned():
    RISK_CFG = risk_common.load_config("run")  # read before the tests change the working directory
CAPITAL = 700000.0


class Replay(World):
    """The synthetic world of the targets tests plus a risk config whose files live in the temp working directory."""

    def setUp(self):
        super().setUp()
        Path("config/analyst.json").write_text(json.dumps(self.cfg), encoding="utf-8")
        Path("config/indices.json").write_text(json.dumps({"indices": ["^NSEI"]}), encoding="utf-8")
        self.risk = copy.deepcopy(RISK_CFG)
        self.risk["paths"].update(risk="data/risk", analyst="data/analyst", analystConfig="config/analyst.json", metadataConfig="config/config.json",
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


class VanishedNames(Replay):
    def setUp(self):
        super().setUp()
        base = self.sim()
        buys = base.fills[base.fills.side == "BUY"]
        sells = base.fills[base.fills.side == "SELL"]
        for f in buys.itertuples():
            i = self.days.index(f.trade_date)
            if i + 6 < self.days.index(self.end) and not ((sells.ticker == f.ticker) & (sells.trade_date <= self.days[i + 6])).any():
                self.ticker, self.last = f.ticker, self.days[i + 2]
                break
        else:
            self.fail("no buy that is held for a week in the synthetic run")
        series = copy.deepcopy(self.data.series)
        series[self.ticker] = series[self.ticker][series[self.ticker].Date <= self.last].reset_index(drop=True)
        self.cut = pit.PitData(series, self.data.index.copy(), self.data.buckets)

    def run_cut(self, haircut):
        return self.sim(data=self.cut, targets=Targets(self.cut, self.cfg), vanish_haircut=haircut)

    def test_the_position_leaves_on_the_first_session_after_its_last_row_at_that_close(self):
        r = self.run_cut(0.0)
        exits = [v for v in r.vanished if v["ticker"] == self.ticker]
        self.assertEqual(len(exits), 1)
        v = exits[0]
        self.assertEqual(v["trade_date"], self.days[self.days.index(self.last) + 1])
        self.assertEqual(v["price"], round(self.cut.last_close(self.ticker), 4))
        self.assertEqual(v["charges"], 0.0)
        self.assertEqual(v["bucket"], next(f["bucket"] for f in r.fills.to_dict("records") if f["ticker"] == self.ticker))
        held = r.fills[r.fills.ticker == self.ticker].assign(q=lambda x: x.qty * (x.side == "BUY").map({True: 1, False: -1})).q.sum()
        self.assertEqual(held, 0)  # the exit is in the fills, so the tax lots see the sale
        self.assertEqual(len([x for x in r.vanished if x["ticker"] == self.ticker]), 1)  # never again

    def test_a_write_off_costs_exactly_the_haircut_and_changes_nothing_before_the_exit(self):
        a, b, c = self.run_cut(0.0), self.run_cut(0.5), self.run_cut(1.0)
        qty = a.vanished[0]["qty"]
        last = a.vanished[0]["lastClose"]
        before = a.nav.date < a.vanished[0]["trade_date"]
        pd.testing.assert_frame_equal(a.nav[before], b.nav[before])
        self.assertEqual(c.vanished[0]["price"], 0.0)
        self.assertAlmostEqual(a.vanished[0]["price"] * qty - b.vanished[0]["price"] * qty, 0.5 * last * qty, delta=0.01 * qty)
        day = a.vanished[0]["trade_date"]
        nav = lambda r: float(r.nav[r.nav.date == day].nav.iloc[0])  # noqa: E731
        self.assertGreater(nav(a), nav(b))
        self.assertGreater(nav(b), nav(c))

    def test_nothing_vanishes_in_the_untouched_world(self):
        self.assertEqual([v for v in self.sim().vanished if v["ticker"] == self.ticker], [])


class Progress(Replay):
    def test_progress_is_reported_about_once_per_percent_and_ends_at_the_last_day(self):
        calls = []
        r = self.sim(progress=lambda done, total, asof: calls.append((done, total, asof)))
        total = len(r.nav)
        self.assertEqual(calls[-1], (total, total, self.end))
        self.assertEqual([c[0] for c in calls], sorted({c[0] for c in calls}))
        self.assertLessEqual(len(calls), total)  # 161 days is under 200, so every day reports; a longer run reports every len//100 days

    def test_the_cli_prints_a_line_per_five_percent_with_an_eta(self):
        import contextlib
        import io

        from backtest import run
        show, out = run._progress("case"), io.StringIO()
        with contextlib.redirect_stdout(out):
            for done in range(1, 201):
                show(done, 200, "2020-01-01")
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 21)  # 0% is never printed; 5%, 10% ... 100%
        self.assertTrue(lines[-1].startswith("[case] 100%") and "eta 0:00:00" in lines[-1])


class OwnerRestart(Replay):
    """The live ladder stays flat-locked until a manual restartFrom; the backtest's owner restarts it by rule."""

    def setUp(self):
        super().setUp()
        self.risk["ladder"]["levels"] = [{"drawdownPct": d, "maxInvestedPct": m} for d, m in ((0.004, 0.75), (0.008, 0.5), (0.012, 0.25), (0.016, 0.0))]
        risk_common.validate(self.risk, "run")

    def test_without_a_restart_nothing_trades_after_the_flat_lock_and_with_one_the_ladder_comes_back(self):
        live = self.sim(keep_signals=True, end=None)
        locked = [s["asOf"] for s in live.signals if s["ladder"]["flatLocked"]]
        self.assertTrue(locked, "the tightened ladder must flat-lock in this world")
        self.assertEqual(live.restarts, [])
        after = live.fills[live.fills.trade_date > locked[0]]
        self.assertTrue((after.side == "SELL").all())  # a locked ladder only sells
        owner = self.sim(keep_signals=True, restart_after=20, end=None)
        self.assertGreater((owner.fills[owner.fills.trade_date > locked[0]].side == "BUY").sum(), 0)  # and the restart lets it buy again
        self.assertTrue(owner.restarts)
        first = owner.restarts[0]
        self.assertGreaterEqual(self.days.index(first) - self.days.index(locked[0]), 20)
        flags = {s["asOf"]: s["ladder"]["flatLocked"] for s in owner.signals}
        self.assertTrue(flags[first] is False)  # the restart takes effect on the day itself
        self.assertEqual(owner.nav.iloc[:self.days.index(locked[0]) - self.days.index(self.start)].to_dict(),
                         live.nav.iloc[:self.days.index(locked[0]) - self.days.index(self.start)].to_dict())  # nothing before the lock changes

    def test_the_rule_waits_for_the_regime_to_recover(self):
        from backtest.replay import _restart_due
        cfg = self.risk
        lad = {"flatLocked": True, "flatLockedSince": self.days[10]}
        days = self.days
        self.assertFalse(_restart_due(lad, days, days[20], 20, cfg, ["BULL", "TREND"]))        # too soon
        self.assertFalse(_restart_due(lad, days, days[40], 20, cfg, ["BULL", "BEAR"]))        # regime not recovered
        self.assertFalse(_restart_due(lad, days, days[40], 20, cfg, ["TREND"]))               # not enough weeks
        self.assertTrue(_restart_due(lad, days, days[40], 20, cfg, ["WEAK", "BULL", "TREND"]))
        self.assertFalse(_restart_due({"flatLocked": False, "flatLockedSince": None}, days, days[40], 20, cfg, ["BULL", "TREND"]))
        self.assertFalse(_restart_due(None, days, days[40], 20, cfg, ["BULL", "TREND"]))
