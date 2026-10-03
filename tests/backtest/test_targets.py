"""Targets parity: backtest.targets.Targets equals `python -m app.analyst.signals --as-of` for every rebalance date of a synthetic world."""

import argparse
import contextlib
import copy
import io
import json
import logging
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from app.analyst import common, signals
from app.analyst.common import Report
from app.market import registry
from app.market.common import COLS, IST
from app.market.store import Store
from backtest import pit
from backtest.targets import PICK_KEYS, Targets

APP_CFG = common.load_config()  # read before the tests change the working directory
META = Path("app/config/config.json").read_text(encoding="utf-8")
LOG = logging.getLogger("test.targets")
NOW = datetime(2026, 9, 25, 21, 30, tzinfo=IST)
BUCKETS = {"LargeCap": ["L1", "L2", "L3", "L4"], "MidCap": ["M1", "M2", "M3", "M4"], "SmallCap": ["S1", "S2", "S3", "S4", "S5"]}


def walk(rng, n, drift, vol, start=100.0):
    return start * np.exp(np.cumsum(rng.normal(drift, vol, n)))


def frame(ticker, days, close, volume=1e7):
    c = pd.Series(close).round(2)
    return pd.DataFrame({"Ticker": ticker, "Date": days, "Open": c, "High": c * 1.01, "Low": c * 0.99, "Close": c, "AdjClose": c, "Volume": int(volume)})[COLS]


class World(unittest.TestCase):
    data_dir = "data"  # where the synthetic prices and bucket files go (the single-run test uses app/data)
    """Index with an up, down, up path (so BULL/BEAR/WEAK/TREND all occur) and 13 tickers with gaps, late starts, an early end and thin volume."""

    @classmethod
    def setUpClass(cls):
        cls.days = [d.date().isoformat() for d in pd.bdate_range("2021-01-01", periods=900)]

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        previous = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        rng, days = np.random.default_rng(7), self.days
        index = np.r_[walk(rng, 450, 0.0015, 0.006, 10000), 0][:-1]
        index = np.r_[index, index[-1] * np.exp(np.cumsum(rng.normal(-0.003, 0.008, 150)))]
        index = np.r_[index, index[-1] * np.exp(np.cumsum(rng.normal(0.002, 0.007, 300)))]
        market, storage = Path(self.data_dir) / "market", Path(self.data_dir) / "storage"
        storage.mkdir(parents=True)
        eq, idx = Store(market, cutoff=""), Store(market / "indices", cutoff="")
        idx.upsert("NSEI", frame("^NSEI", days, index))
        reg = registry.load(market / "registry.csv")
        registry.refresh(reg, {s: days[-1] for ss in BUCKETS.values() for s in ss})
        registry.save(reg, market / "registry.csv")
        for b, symbols in BUCKETS.items():
            pd.DataFrame({"Symbol": symbols}).to_csv(storage / f"{b}_{days[0]}.csv", index=False)
            for i, s in enumerate(symbols):
                first, last = (250 if s == "M3" else 0), (700 if s == "S4" else 900)  # a late start, an early end
                close = walk(rng, last - first, 0.0008 + 0.0004 * (i % 3), 0.012 + 0.004 * (i % 2))
                df = frame(s, days[first:last], close, volume=1e3 if s == "L4" else 1e7)  # L4 is illiquid
                if s == "S2":
                    df = df.drop(df.index[[300, 301, 560]]).reset_index(drop=True)  # halted days
                eq.upsert(s, df)
        self.cfg = copy.deepcopy(APP_CFG)
        self.cfg["placeholders"] = False
        self.cfg["selector"].update(maxBucketFileAgeDays=10**6, maxMissingShare=1.0)
        self.cfg["paths"] = {k: str(self.root / v) for k, v in {
            "analyst": "data/analyst", "market": "data/market", "upstreamStorage": "data/storage",
            "metadataConfig": "config/config.json", "calendar": "config/cal.json", "seed": "config/seed.csv", "logs": "data/logs"}.items()}
        Path("config").mkdir()
        Path("config/config.json").write_text(META, encoding="utf-8")
        Path("config/cal.json").write_text(json.dumps({"holidays": [], "specialSessions": []}), encoding="utf-8")
        self.data = pit.PitData.load(self.data_dir, list(BUCKETS))

    def live(self, as_of: str) -> dict:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            signals.run(self.cfg, NOW, LOG, Report("signals"), argparse.Namespace(force=False, as_of=as_of, check=False))
        return json.loads(out.getvalue())

    def view(self, t: dict) -> dict:
        return {"regime": t["regime"], "composition": t["composition"],
                "buckets": {b: {"strategy": e["strategy"], "selected": [{k: p[k] for k in PICK_KEYS if k in p} for p in e["selected"]]}
                            for b, e in t["buckets"].items()}}


class ParityTests(World):
    def test_every_rebalance_date_matches_the_live_replay(self):
        tg = Targets(self.data, self.cfg)
        dates = [d for d in tg.dates if d >= self.days[260]]
        regimes, picked = set(), 0
        for d in dates:
            mine, live = tg.build(d), self.live(d)
            self.assertEqual(self.view(mine), self.view(live), d)
            regimes.add(mine["regime"]["active"])
            picked += sum(len(e["selected"]) for e in mine["buckets"].values())
        self.assertGreaterEqual(regimes, {"BULL", "BEAR"}, regimes)  # the world exercises more than one regime
        self.assertGreater(picked, 50)

    def test_parity_holds_with_tuned_windows_and_strategy(self):
        self.cfg["regime"].update(smaFast=30, smaSlow=120, momentumDays=40, minRows=200)
        self.cfg["strategies"]["BULL"]["LargeCap"].update(top_n=3, lookback=60)
        self.cfg["selector"]["liquidity"]["minAdvCr"] = 0.5
        tg = Targets(self.data, self.cfg)
        for d in [d for d in tg.dates if d >= self.days[250]][::3]:
            self.assertEqual(self.view(tg.build(d)), self.view(self.live(d)), d)

    def test_bear_composite_ranking_matches(self):
        tg = Targets(self.data, self.cfg)
        bears = [d for d in tg.dates if tg.build(d)["regime"]["active"] == "BEAR"]
        self.assertTrue(bears)
        for d in bears[::4]:
            self.assertEqual(self.view(tg.build(d)), self.view(self.live(d)), d)


class BehaviourTests(World):
    def test_regimes_are_causal_and_sliced_by_date(self):
        tg = Targets(self.data, self.cfg)
        cur, hist = tg.regimes(self.days[-1])
        self.assertEqual(hist, tg.active)
        d = tg.dates[100]
        cur, hist = tg.regimes(d)
        self.assertEqual(len(hist), 101)
        self.assertEqual(cur["active"], hist[-1])
        before = tg.regimes("1999-01-01")
        self.assertEqual(before, ({"raw": "Unknown", "active": "Unknown"}, []))

    def test_no_targets_for_a_date_that_is_not_a_rebalance_date(self):
        tg = Targets(self.data, self.cfg)
        self.assertIsNone(tg.build("1999-01-04"))

    def test_future_prices_cannot_change_a_past_selection(self):
        tg = Targets(self.data, self.cfg)
        d = tg.dates[120]
        before = tg.build(d)
        self.assertTrue(any(e["selected"] for e in before["buckets"].values()))
        poisoned = copy.deepcopy(self.data.series)
        for df in poisoned.values():
            df.loc[df.Date > d, ["AdjClose", "Close"]] *= 50
        index = self.data.index.copy()
        index.loc[index.Date > d, "Close"] *= 0.1
        after = Targets(pit.PitData(poisoned, index, self.data.buckets), self.cfg).build(d)
        self.assertEqual(before, after)

    def test_unknown_regime_selects_nothing(self):
        tg = Targets(self.data, self.cfg)
        early = next(d for d in tg.dates if tg.build(d)["regime"]["active"] == "Unknown")
        self.assertTrue(all(e["selected"] == [] for e in tg.build(early)["buckets"].values()))

    def test_too_short_index_is_refused(self):
        short = pit.PitData(self.data.series, self.data.index.iloc[:50].reset_index(drop=True), self.data.buckets)
        with self.assertRaises(ValueError):
            Targets(short, self.cfg)


if __name__ == "__main__":
    unittest.main()
