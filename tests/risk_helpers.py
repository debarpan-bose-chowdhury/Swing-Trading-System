"""Shared fixtures for the Risk Manager tests: a synthetic data tree (prices, buckets, ledger, targets, status files)."""

import argparse
import copy
import json
import logging
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from app.market.common import COLS, IST
from app.market.store import Store
from app.risk import run as risk_run
from app.risk.common import Context, Portfolio, Report, load_config, read_json
from app.market.tradingcal import Calendar

LOG = logging.getLogger("test.risk")
CFG = load_config("run")  # read before the tests change the working directory
META = Path("app/config/config.json").read_text(encoding="utf-8")
ANALYST = Path("app/config/analyst.json").read_text(encoding="utf-8")
FRIDAY, THURSDAY = "2026-09-25", "2026-09-24"
NOW = datetime(2026, 9, 25, 21, 45, tzinfo=IST)
BOOK_COLS = ["ticker", "qty", "avg_price", "entry_date", "entry_source", "last_reconciled"]


def bars(ticker: str, days, closes=None, rng: float = 9.0, base: float = 250.0, volume: float = 1e7, adj=None, opens=None) -> pd.DataFrame:
    """Constant-range bars: High-Low = rng, so ATR20 = rng while closes stay flat."""
    c = np.full(len(days), base) if closes is None else np.asarray(closes, float)
    return pd.DataFrame({"Ticker": ticker, "Date": list(days), "Open": c if opens is None else opens, "High": c + rng / 2, "Low": c - rng / 2,
                         "Close": c, "AdjClose": c if adj is None else np.asarray(adj, float), "Volume": int(volume)})[COLS]


class Env(unittest.TestCase):
    asof = FRIDAY

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        previous = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        self.cfg = copy.deepcopy(CFG)
        self.cfg["paths"] = {k: str(self.root / v) for k, v in {
            "risk": "data/risk", "market": "data/market", "analyst": "data/analyst", "upstreamStorage": "data/storage",
            "metadataConfig": "config/config.json", "analystConfig": "config/analyst.json", "calendar": "config/cal.json",
            "indices": "config/indices.json", "logs": "data/logs"}.items()}
        self.cfg["capital"]["cashFlowsFile"] = "config/cash_flows.csv"
        (self.root / "config").mkdir()
        (self.root / "config/config.json").write_text(META, encoding="utf-8")
        (self.root / "config/analyst.json").write_text(ANALYST, encoding="utf-8")
        (self.root / "config/indices.json").write_text(json.dumps({"indices": ["^NSEI"]}))
        self.set_calendar(["2026-10-02"])
        self.market, self.analyst, self.risk = (self.root / "data" / n for n in ("market", "analyst", "risk"))
        self.store, self.idx_store = Store(self.market, cutoff=""), Store(self.market / "indices", cutoff="")
        self.days = [d.date().isoformat() for d in pd.bdate_range(end=FRIDAY, periods=400)]
        self.cfg["mail"] = {"smtpHost": "<set at deployment>", "smtpPort": 587, "sender": "<set at deployment>", "recipients": ["<set at deployment>"]}
        self.index()
        self.market_status()
        self.ledger_status()
        self.flows([("2026-01-01", "OPENING", 700000)])
        self.buckets(SmallCap=[], MidCap=[], LargeCap=[])
        self.regimes([(FRIDAY, "BULL", "BULL")])
        self.book([])

    # --- inputs ---
    def set_calendar(self, holidays=(), special=()):
        Path(self.cfg["paths"]["calendar"]).write_text(json.dumps({"holidays": list(holidays), "specialSessions": list(special)}))

    def index(self, closes=None, days=None):
        days = days or self.days
        self.idx_store.upsert("NSEI", bars("^NSEI", days, closes, rng=0.0, base=20000.0))

    def market_status(self, status="ok", last=FRIDAY, lock=False):
        self.market.mkdir(parents=True, exist_ok=True)
        (self.market / "status.json").write_text(json.dumps({"status": status, "stage": "updator", "lastTradingDay": last}))
        (self.market / ".lock").unlink(missing_ok=True)
        if lock:
            (self.market / ".lock").write_text("1")

    def ledger_status(self, status="ok", good=FRIDAY, untracked=()):
        self.analyst.mkdir(parents=True, exist_ok=True)
        (self.analyst / "analyst_status.json").write_text(json.dumps({"ledger": {"status": status, "lastGoodRunDate": good, "untracked": list(untracked)}}))

    def flows(self, rows):
        pd.DataFrame(rows, columns=["date", "type", "amount_inr"]).assign(note="").to_csv("config/cash_flows.csv", index=False)

    def buckets(self, day="2026-09-21", **symbols):
        storage = Path(self.cfg["paths"]["upstreamStorage"])
        storage.mkdir(parents=True, exist_ok=True)
        for b, syms in symbols.items():
            pd.DataFrame({"Symbol": syms, "MarketCap": 1, "InceptionDate": "2000-01-01"}).to_csv(storage / f"{b}_{day}.csv", index=False)

    def regimes(self, rows):
        (self.analyst / "regime").mkdir(parents=True, exist_ok=True)
        pd.DataFrame(rows, columns=["date", "raw_regime", "active_regime"]).assign(pending_regime="", pending_remaining_days=0).to_csv(
            self.analyst / "regime/regime_history.csv", index=False)

    def price(self, ticker, closes=None, **kw):
        days = kw.pop("days", self.days)
        self.store.fresh(ticker).unlink(missing_ok=True)  # a price call always replaces the history
        self.store.upsert(ticker, bars(ticker, days, closes, **kw))

    def book(self, rows):
        """rows: (ticker, qty, avg_price, entry_date[, entry_source])."""
        (self.analyst / "ledger").mkdir(parents=True, exist_ok=True)
        df = pd.DataFrame([(r[0], r[1], r[2], r[3], r[4] if len(r) > 4 else ("UNKNOWN" if r[3] == "UNKNOWN" else "FILLS"), "") for r in rows], columns=BOOK_COLS)
        df.to_csv(self.analyst / "ledger/book.csv", index=False)

    def fills(self, rows):
        """rows: (trade_date, ticker, side, qty, price[, kind])."""
        cols = ["fill_key", "trade_date", "ticker", "broker_symbol", "side", "qty", "price", "fill_time", "order_id", "run_id", "kind"]
        df = pd.DataFrame([(f"k{i}", r[0], r[1], r[1], r[2], r[3], r[4], "", "", "r", r[5] if len(r) > 5 else "FILL") for i, r in enumerate(rows)], columns=cols)
        (self.analyst / "ledger").mkdir(parents=True, exist_ok=True)
        df.to_csv(self.analyst / "ledger/fills.csv", index=False)

    def targets(self, selected=None, regime=("BULL", "BULL"), comp=None, day=FRIDAY, top_n=2, ma=150, status="ok", schema=1, strategy=True):
        """selected: {bucket: [tickers]}; the Analyst's target-file shape (only the fields the Risk Manager reads)."""
        comp = comp or {"LargeCap": 0, "MidCap": 0, "SmallCap": 1}
        selected = selected or {}
        buckets = {b: {"strategy": {"top_n": top_n, "lookback": 126, "stock_trend_ma": ma} if strategy else None,
                       "selected": [{"ticker": t, "rank": i, "price": 250.0, "trendMa": 240.0} for i, t in enumerate(selected.get(b, []), 1)]} for b in comp}
        t = {"schemaVersion": schema, "status": status, "rebalanceDate": day, "regime": {"raw": regime[0], "active": regime[1]},
             "composition": comp, "buckets": buckets, "limits": {"maxPositionDrawdownPct": 0.17}}
        folder = self.analyst / "targets"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"targets_{day}.json").write_text(json.dumps(t))
        return t

    def surveillance(self, day=FRIDAY, gsm=(), t2t=(), asm=None, bands=None, sources=None):
        folder = self.risk / "surveillance"
        folder.mkdir(parents=True, exist_ok=True)
        data = {"asOf": day, "fetchedAt": f"{day}T20:17:00+05:30", "sources": sources or dict.fromkeys(("asm", "gsm", "t2t", "bands"), "ok"),
                "asm": {"LT": asm or {}, "ST": {}}, "gsm": dict.fromkeys(gsm, 1), "t2t": list(t2t), "bandPct": bands or {}}
        (folder / f"surveillance_{day}.json").write_text(json.dumps(data))

    # --- helpers ---
    def set_day(self, asof, targets_expected=False):
        """Make asof the run day: statuses, ledger and the run time (21:45 IST) all point at it."""
        self.asof = asof
        self.market_status(last=asof)
        self.ledger_status(good=asof)
        return datetime.fromisoformat(f"{asof}T21:45:00+05:30")

    def days_to(self, asof):
        return [d for d in self.days if d <= asof]

    def context(self, asof=None, targets=None, last_good=None, windows=None, rebalance=None) -> Context:
        asof = asof or self.asof
        cal = Calendar(self.cfg["paths"]["calendar"])
        ctx = Context(self.cfg, asof, cal, Store(self.market, cutoff=""), LOG, risk_run.bucket_symbols(self.cfg, asof), last_good=last_good,
                      targets=targets, windows=windows or {}, surv=__import__("app.risk.surveil", fromlist=["load"]).load(self.cfg, cal, asof),
                      rebalance=targets is not None if rebalance is None else rebalance)
        return ctx

    def portfolio(self, book=None, cash=700000.0) -> Portfolio:
        df = pd.read_csv(self.analyst / "ledger/book.csv", dtype=str, keep_default_na=False) if book is None else book
        df = df.astype({"qty": "int64", "avg_price": "float64"})
        return Portfolio("actual", self.risk / "state", self.risk / "nav" / "nav_actual.csv", self.risk / "signals", df, cash)

    def run_risk(self, now=NOW, force=False):
        report = Report("run")
        risk_run.run(self.cfg, now, LOG, report, argparse.Namespace(force=force, check=False))
        return report

    def signals(self, day=None):
        return read_json(self.risk / "signals" / f"signals_{day or self.asof}.json")

    def actions(self, day=None):
        return {a["ticker"]: a for a in self.signals(day)["actions"]}
