"""Fill parity: backtest.fills equals app.risk.shadow.apply, day by day, on scripted signals (fills, book, cash, warnings)."""

import copy
import json
import logging
import os
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from app.analyst import common, costs
from app.market.common import COLS
from app.risk import shadow
from app.risk.common import Context, Portfolio
from backtest import fills, pit

COSTS = common.load_config()["costs"]  # read before the tests change the working directory
LOG = logging.getLogger("test.fills")
BUCKETS = {"LargeCap": ["L1", "L2"], "MidCap": ["M1", "M2"], "SmallCap": ["S1", "S2", "S3"]}


class CostParity(unittest.TestCase):
    def test_round_trip_on_one_lakh_is_293_28(self):
        self.assertEqual(round(costs.buy_charges(COSTS, 100000) + costs.sell_charges(COSTS, 100000), 2), 293.28)


class FillParity(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        previous = os.getcwd()
        os.chdir(Path(tmp.name).resolve())
        self.addCleanup(os.chdir, previous)
        self.rng = np.random.default_rng(11)
        self.days = [d.date().isoformat() for d in pd.bdate_range("2024-01-01", periods=130)]
        series = {}
        for b, symbols in BUCKETS.items():
            for s in symbols:
                close = 200 * np.exp(np.cumsum(self.rng.normal(0, 0.02, len(self.days))))
                openp = close * (1 + self.rng.normal(0, 0.01, len(self.days)))
                df = pd.DataFrame({"Ticker": s, "Date": self.days, "Open": openp, "High": close * 1.02, "Low": close * 0.98, "Close": close,
                                   "AdjClose": close, "Volume": 10**6})[COLS]
                if s == "M1":
                    df = df.drop(df.index[[40, 41, 42]]).reset_index(drop=True)  # no Open for a few days: the order is retried
                if s == "S3":
                    df = df.drop(df.index[range(60, 75)]).reset_index(drop=True)  # gone for more than 7 days: the order lapses
                series[s] = df.round(2)
        self.data = pit.PitData(series, series["L1"].assign(Ticker="^NSEI"), BUCKETS)
        self.store = pit.PitStore(self.data)
        self.cfg = {"paths": {"risk": "data/risk"}, "costs": COSTS}
        self.bucket_of = {s: b for b, ss in BUCKETS.items() for s in ss}

    def signal(self, i: int, book: fills.Book, prev: dict | None) -> dict:
        """Random actions for day i: sells (some larger than held), big and small buys, and a repeat of yesterday's sells."""
        acts = []
        for t, p in sorted(book.pos.items()):
            if self.rng.random() < 0.3:
                acts.append({"ticker": t, "bucket": self.bucket_of[t], "side": "SELL", "qty": int(p["qty"] * self.rng.choice([0.5, 1.0, 1.5]))})
        if prev:
            acts += [a for a in prev["actions"] if a["side"] == "SELL"]  # a STOP repeats until the position is gone
        for t in self.rng.choice(sorted(self.bucket_of), size=3, replace=False):
            acts.append({"ticker": str(t), "bucket": self.bucket_of[t], "side": "BUY", "qty": int(self.rng.choice([10, 50, 400, 5000]))})
        nxt = self.days[min(i + 1, len(self.days) - 1)]
        return {"asOf": self.days[i], "executionDate": nxt, "actions": acts}

    def test_day_by_day_against_shadow_apply(self):
        empty = pd.DataFrame(columns=fills.BOOK_COLS)
        shadow.seed(self.cfg, Portfolio("actual", Path("s"), Path("n"), Path("g"), empty, 100000.0), self.days[0])
        mine, queue, prev = fills.Book(100000.0), [], None
        sig_dir = shadow.root(self.cfg) / "signals"
        sig_dir.mkdir(parents=True)
        seen = {"fills": 0, "NO_OPEN": 0, "SHORTFALL": 0}
        for i, day in enumerate(self.days[:-1]):
            self.store.asof = day
            ctx = Context(self.cfg, day, None, self.store, LOG, {})
            pf = shadow.apply(ctx)
            queue = [s for s in queue if s["asOf"] < day]
            new, warns, cash = fills.execute(mine, COSTS, day, queue, lambda t: self.data.open_price(t, day), shadow.LOOKBACK_DAYS)
            queue = [s for s in queue if (pd.Timestamp(day) - pd.Timestamp(s["asOf"])).days <= shadow.LOOKBACK_DAYS]

            live = shadow.read_fills(shadow.root(self.cfg) / "fills.csv")
            live = live[(live.trade_date == day) & (live.kind == "FILL")]
            self.assertEqual([(f["ticker"], f["side"], f["qty"], f["price"]) for f in new],
                             list(zip(live.ticker, live.side, live.qty, live.price, strict=True)), day)
            pd.testing.assert_frame_equal(mine.frame(), pf.book.reset_index(drop=True), check_dtype=False, obj=f"book {day}")
            self.assertEqual(cash, pf.cash, day)
            stored = pd.read_csv(shadow.root(self.cfg) / "cash.csv", dtype=str)
            self.assertEqual(f"{mine.cash:.2f}", f"{float(stored.cash.iloc[-1]):.2f}", day)
            self.assertEqual(sorted(warns), sorted(ctx.warnings), day)
            seen["fills"] += len(new)
            seen["NO_OPEN"] += any(w.startswith("SHADOW_NO_OPEN") for w in warns)
            seen["SHORTFALL"] += any(w.startswith("SHADOW_SHORTFALL") for w in warns)

            sig = self.signal(i, mine, prev)
            (sig_dir / f"signals_{day}.json").write_text(json.dumps({**sig, "schemaVersion": 1}))
            queue.append(copy.deepcopy(sig))
            prev = sig
        self.assertGreater(seen["fills"], 40)
        self.assertGreater(seen["NO_OPEN"], 0)
        self.assertGreater(seen["SHORTFALL"], 0)


class ExecuteBehaviour(unittest.TestCase):
    def sig(self, side="BUY", qty=10, asof="2024-01-02", execution="2024-01-03"):
        return {"asOf": asof, "executionDate": execution, "actions": [{"ticker": "AAA", "bucket": "LargeCap", "side": side, "qty": qty}]}

    def test_buy_fills_at_open_plus_slippage_and_pays_charges(self):
        b = fills.Book(100000.0)
        new, warns, cash = fills.execute(b, COSTS, "2024-01-03", [self.sig()], lambda t: 100.0, 7)
        price = 100.0 * (1 + COSTS["slippageBpsPerSide"]["LargeCap"] / 10000)
        self.assertEqual((new[0]["qty"], new[0]["price"]), (10, round(price, 4)))
        self.assertAlmostEqual(cash, 100000 - (10 * price + costs.buy_charges(COSTS, 10 * price)), places=8)
        self.assertEqual(b.frame().to_dict("records"), [{"ticker": "AAA", "qty": 10, "avg_price": round(price, 4), "entry_date": "2024-01-03", "entry_source": "FILLS"}])

    def test_signal_not_yet_due_or_already_executed_does_nothing(self):
        b = fills.Book(100000.0)
        self.assertEqual(fills.execute(b, COSTS, "2024-01-02", [self.sig()], lambda t: 100.0, 7)[0], [])  # same day as asOf
        fills.execute(b, COSTS, "2024-01-03", [self.sig()], lambda t: 100.0, 7)
        self.assertEqual(fills.execute(b, COSTS, "2024-01-04", [self.sig()], lambda t: 100.0, 7)[0], [])  # dedupe

    def test_sell_is_capped_at_the_book_and_removes_the_position(self):
        b = fills.Book(100000.0)
        fills.execute(b, COSTS, "2024-01-03", [self.sig()], lambda t: 100.0, 7)
        new, _, _ = fills.execute(b, COSTS, "2024-01-05", [self.sig("SELL", 99, "2024-01-04", "2024-01-05")], lambda t: 110.0, 7)
        self.assertEqual(new[0]["qty"], 10)
        self.assertTrue(b.frame().empty)

    def test_lapsed_signal_is_dropped(self):
        b = fills.Book(100000.0)
        new, warns, _ = fills.execute(b, COSTS, "2024-01-12", [self.sig()], lambda t: 100.0, 7)
        self.assertEqual(new, [])
        self.assertEqual(warns, [])


if __name__ == "__main__":
    unittest.main()
