"""Signals end to end on a synthetic data tree: gates, regime, selection, delta, target file, replay."""

import argparse
import copy
import json
import logging
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import pandas as pd

from app.analyst import common, signals
from app.analyst.common import Gate, Report
from app.market import registry
from app.market.common import COLS, IST
from app.market.store import Store

LOG = logging.getLogger("test.signals")
CFG = common.load_config()  # read before the tests change the working directory
META = Path("app/config/config.json").read_text(encoding="utf-8")
FRIDAY = "2026-09-25"
NOW = datetime(2026, 9, 25, 21, 30, tzinfo=IST)
SUNDAY = datetime(2026, 9, 27, 21, 30, tzinfo=IST)


def args(**kw):
    return argparse.Namespace(force=kw.get("force", False), as_of=kw.get("as_of"), check=False)


def rows(ticker: str, days, start: float, step: float, volume: float = 1e7) -> pd.DataFrame:
    price = [start + step * i for i in range(len(days))]
    return pd.DataFrame({"Ticker": ticker, "Date": days, "Open": price, "High": price, "Low": price, "Close": price,
                         "AdjClose": price, "Volume": int(volume)})[COLS]


class Env(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        previous = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        self.cfg = copy.deepcopy(CFG)
        self.cfg["placeholders"] = False
        self.cfg["composition"] = {"LargeCap": 0, "MidCap": 0, "SmallCap": 1}  # pinned: tests must not follow the live config
        self.cfg["paths"] = {k: str(self.root / v) for k, v in {
            "analyst": "data/analyst", "market": "data/market", "upstreamStorage": "data/storage",
            "metadataConfig": "config/config.json", "calendar": "config/cal.json", "seed": "config/seed.csv", "logs": "data/logs"}.items()}
        (self.root / "config").mkdir()
        (self.root / "config/config.json").write_text(META, encoding="utf-8")
        self.set_calendar(["2026-10-02"])
        self.market, self.analyst = self.root / "data/market", self.root / "data/analyst"
        self.store = Store(self.market, cutoff="")
        self.idx_store = Store(self.market / "indices", cutoff="")
        self.days = [d.date().isoformat() for d in pd.bdate_range(end=FRIDAY, periods=400)]
        self.index(self.days)
        self.market_status()
        self.tickers = {}

    def set_calendar(self, holidays):
        (self.root / "config/cal.json").write_text(json.dumps({"holidays": holidays, "specialSessions": []}), encoding="utf-8")

    def index(self, days):
        self.idx_store.upsert("NSEI", rows("^NSEI", days, 10000, 10))

    def market_status(self, **over):
        self.market.mkdir(parents=True, exist_ok=True)
        data = {"status": "ok", "stage": "updator", "lastTradingDay": FRIDAY, **over}
        (self.market / "status.json").write_text(json.dumps(data), encoding="utf-8")

    def small_cap(self, specs: dict, inactive=()):
        """specs: ticker -> (step, volume, last_day). Writes prices, the bucket file and the registry."""
        storage = self.root / "data/storage"
        storage.mkdir(parents=True, exist_ok=True)
        for b in ("LargeCap", "MidCap"):
            pd.DataFrame({"Symbol": [f"{b[:2].upper()}X"]}).to_csv(storage / f"{b}_{FRIDAY}.csv", index=False)
        pd.DataFrame({"Symbol": list(specs)}).to_csv(storage / f"SmallCap_{FRIDAY}.csv", index=False)
        reg = registry.load(self.market / "registry.csv")
        registry.refresh(reg, {s: FRIDAY for s in [*specs, "LAX", "MIX"]})
        for t in inactive:
            reg.loc[t, "status"] = "inactive"
        registry.save(reg, self.market / "registry.csv")
        for t, (step, volume, last_day) in specs.items():
            days = [d for d in self.days[-220:] if d <= last_day]
            self.store.upsert(t, rows(t, days, 100, step, volume))

    def run_signals(self, now=NOW, **kw):
        report = Report("signals")
        signals.run(self.cfg, now, LOG, report, args(**kw))
        return report

    def targets(self) -> dict:
        return json.loads((self.analyst / "targets" / f"targets_{FRIDAY}.json").read_text(encoding="utf-8"))

    def ledger(self, positions: list[tuple], ltp: dict, good="2026-09-25", untracked=()):
        (self.analyst / "ledger").mkdir(parents=True, exist_ok=True)
        pd.DataFrame(positions, columns=["ticker", "qty", "avg_price"]).to_csv(self.analyst / "ledger/book.csv", index=False)
        (self.analyst / "snapshots").mkdir(exist_ok=True)
        snap = {"fetchedAt": good, "endpoint": "holdings", "rows": [{"tradingsymbol": f"{t}-EQ", "ltp": p} for t, p in ltp.items()]}
        (self.analyst / f"snapshots/holdings_{good}.json").write_text(json.dumps(snap), encoding="utf-8")
        (self.analyst / "analyst_status.json").write_text(json.dumps({"ledger": {"lastGoodRunDate": good, "untracked": list(untracked)}}), encoding="utf-8")


class TargetsTests(Env):
    SPECS = {"FAST": (3.0, 1e7, FRIDAY), "MID": (2.0, 1e7, FRIDAY), "SLOW": (1.0, 1e7, FRIDAY), "OLD": (2.5, 1e7, "2026-09-24"),
             "GONE": (2.0, 1e7, FRIDAY), "THIN": (2.2, 1e3, FRIDAY)}

    def setUp(self):
        super().setUp()
        self.cfg["selector"]["maxMissingShare"] = 0.2  # OLD has no row on the date: 1 of 6
        self.small_cap(self.SPECS, inactive=["GONE"])

    def test_writes_the_target_file_with_the_agreed_schema(self):
        report = self.run_signals()
        t = self.targets()
        self.assertEqual((t["schemaVersion"], t["status"], t["rebalanceDate"], t["executionDate"]), (1, "ok", FRIDAY, "2026-09-28"))
        self.assertEqual(t["regime"]["active"], "BULL")
        self.assertEqual(t["regime"]["persistenceWeeks"], 4)
        small = t["buckets"]["SmallCap"]
        self.assertEqual([p["ticker"] for p in small["selected"]], ["FAST", "MID"])  # top_n 2 in BULL
        self.assertEqual([p["rank"] for p in small["selected"]], [1, 2])
        self.assertEqual(small["excluded"], {"inactive": 1, "noRowOnRebalanceDate": 1, "insufficientHistory": 0, "noPriceData": 0, "illiquid": 1})
        self.assertEqual(small["universe"], 6)
        self.assertEqual(t["buckets"]["LargeCap"]["selected"], [])  # composition weight 0: no selection
        self.assertEqual(t["composition"], {"LargeCap": 0, "MidCap": 0, "SmallCap": 1})
        self.assertEqual(t["limits"], {"maxPositionDrawdownPct": 0.17, "maxPortfolioDrawdownPct": 0.5})
        self.assertEqual(t["delta"], {"available": False, "drop": []})  # the Ledger has not run
        self.assertIsNone(small["selected"][0]["status"])
        self.assertGreater(small["selected"][0]["estRoundTripCostInr"], 0)
        self.assertEqual(small["selected"][0]["refNotionalInr"], 20000)
        self.assertTrue((self.analyst / "regime/regime_history.csv").exists())
        self.assertEqual((report.block["selectedCount"], report.block["targetsFile"]), (2, f"targets/targets_{FRIDAY}.json"))
        self.assertTrue(any("holdings unavailable" in line for line in report.lines))

    def test_delta_keep_add_drop_capital_and_costs(self):
        self.ledger([("FAST", 10, 300.0), ("OLDHOLD", 20, 50.0), ("LAX", 5, 400.0)], {"FAST": 320.0, "OLDHOLD": 40.0, "LAX": 410.0}, untracked=["ABCD"])
        # OLDHOLD must be a known symbol in a bucket file to get a bucket; LAX sits in the weight-0 LargeCap bucket
        pd.DataFrame({"Symbol": ["LAX", "OLDHOLD"]}).to_csv(self.root / f"data/storage/LargeCap_{FRIDAY}.csv", index=False)
        reg = registry.load(self.market / "registry.csv")
        registry.refresh(reg, {"OLDHOLD": FRIDAY})
        registry.save(reg, self.market / "registry.csv")
        self.run_signals()
        t = self.targets()
        statuses = {p["ticker"]: p["status"] for p in t["buckets"]["SmallCap"]["selected"]}
        self.assertEqual(statuses, {"FAST": "KEEP", "MID": "ADD"})
        keep = t["buckets"]["SmallCap"]["selected"][0]
        self.assertEqual(keep["refNotionalInr"], 3200.0)  # 10 shares at the snapshot ltp
        drops = {d["ticker"]: d for d in t["delta"]["drop"]}
        self.assertEqual(set(drops), {"OLDHOLD", "LAX"})
        self.assertEqual((drops["LAX"]["bucket"], drops["LAX"]["reason"]), ("LargeCap", "NO_ALLOCATION"))
        self.assertEqual(drops["OLDHOLD"]["reason"], "NO_ALLOCATION")  # it too is listed in the weight-0 LargeCap file
        self.assertEqual((drops["LAX"]["qty"], drops["LAX"]["avgCost"], drops["LAX"]["ltp"]), (5, 400.0, 410.0))
        self.assertGreater(drops["LAX"]["estExitCostInr"], 0)
        cap = t["capital"]
        self.assertEqual((cap["investedCostInr"], cap["investedMarketValueInr"], cap["holdingsAsOf"]), (3000 + 1000 + 2000, 3200 + 800 + 2050, "2026-09-25"))
        self.assertEqual(cap["totalInr"], cap["floatingInr"] + cap["investedCostInr"])
        self.assertEqual(t["untracked"], ["ABCD"])

    def test_held_ticker_in_no_bucket_file_is_dropped_as_not_selected_with_null_bucket(self):
        self.ledger([("ORPHAN", 1, 10.0)], {"ORPHAN": 11.0})
        self.run_signals()
        d = self.targets()["delta"]["drop"][0]
        self.assertEqual((d["ticker"], d["bucket"], d["reason"]), ("ORPHAN", None, "NOT_SELECTED"))

    def test_stale_holdings_snapshot_omits_delta_even_with_a_fresh_ledger_date(self):
        self.ledger([("FAST", 10, 300.0)], {"FAST": 320.0})
        (self.analyst / "snapshots/holdings_2026-09-25.json").rename(self.analyst / "snapshots/holdings_2026-09-10.json")
        self.run_signals()
        self.assertFalse(self.targets()["delta"]["available"])

    def test_stale_ledger_omits_delta(self):
        self.ledger([("FAST", 10, 300.0)], {"FAST": 320.0}, good="2026-09-20")
        report = self.run_signals()
        t = self.targets()
        self.assertEqual(t["delta"], {"available": False, "drop": []})
        self.assertTrue(any("holdings unavailable" in line for line in report.lines))

    def test_unknown_regime_drops_every_holding(self):
        self.index(self.days[-215:])  # 215 rows: raw regime exists but is never activated; archive of 400 replaced below
        for f in (self.market / "indices/fresh").glob("*.csv"):
            f.unlink()
        self.index(self.days[-215:])
        self.ledger([("FAST", 10, 300.0)], {"FAST": 320.0})
        self.cfg["regime"]["persistenceWeeks"] = 40
        self.run_signals()
        t = self.targets()
        self.assertEqual(t["regime"]["active"], "Unknown")
        self.assertEqual(t["buckets"]["SmallCap"]["selected"], [])
        self.assertEqual(t["delta"]["drop"][0]["reason"], "UNKNOWN_REGIME")

    def test_bear_regime_uses_the_composite_ranking(self):
        close = [10000 - 10 * i for i in range(400)]  # steady decline: raw BEAR, activates at once
        df = rows("^NSEI", self.days, 10000, -10)
        df["Close"] = close
        self.idx_store.upsert("NSEI", df)
        self.run_signals()
        t = self.targets()
        self.assertEqual(t["regime"]["active"], "BEAR")
        picks = t["buckets"]["SmallCap"]["selected"]
        self.assertEqual({p["ticker"] for p in picks}, {"FAST", "MID", "SLOW"})  # the only qualifiers; top_n is 8
        self.assertTrue(all("score" in p for p in picks))


class GateTests(Env):
    def setUp(self):
        super().setUp()
        self.small_cap({"FAST": (3.0, 1e7, FRIDAY), "MID": (2.0, 1e7, FRIDAY)})

    def test_placeholders_and_empty_calendar_fail_the_run(self):
        self.cfg["placeholders"] = True
        with self.assertRaisesRegex(ValueError, "placeholders"):
            self.run_signals()
        self.cfg["placeholders"] = False
        self.set_calendar([])
        with self.assertRaisesRegex(ValueError, "no holidays"):
            self.run_signals()

    def test_ticker_data_not_ready_is_a_gate_not_a_failure(self):
        (self.market / ".lock").write_text("1 x")
        with self.assertRaisesRegex(Gate, "in progress"):
            self.run_signals()
        (self.market / ".lock").unlink()
        for status in ({"status": "failed"}, {"lastTradingDay": "2026-09-24"}):
            self.market_status(**status)
            with self.assertRaises(Gate):
                self.run_signals()
        (self.market / "status.json").unlink()
        with self.assertRaises(Gate):
            self.run_signals()

    def test_partial_status_and_an_archiver_status_without_a_trading_day_pass(self):
        for status in ({"status": "partial"}, {"stage": "archiver", "lastTradingDay": None}):
            self.market_status(**status)
            self.run_signals(force=True)
            self.assertTrue((self.analyst / "targets" / f"targets_{FRIDAY}.json").exists())

    def test_archiver_status_does_not_hide_missing_data(self):
        self.market_status(stage="archiver", lastTradingDay=None)
        for f in (self.market / "indices/fresh").glob("*.csv"):
            f.unlink()
        self.index(self.days[:-1])
        with self.assertRaisesRegex(Gate, "no row"):
            self.run_signals()

    def test_stale_file_of_an_unused_bucket_does_not_fail_the_run(self):
        (self.root / f"data/storage/LargeCap_{FRIDAY}.csv").rename(self.root / "data/storage/LargeCap_2026-01-02.csv")
        report = self.run_signals()
        self.assertTrue((self.analyst / "targets" / f"targets_{FRIDAY}.json").exists())
        self.assertTrue(any(line.startswith("WARNING LargeCap") and "older than" in line for line in report.lines))
        self.cfg["composition"] = {"LargeCap": 0.5, "MidCap": 0, "SmallCap": 0.5}
        with self.assertRaisesRegex(ValueError, "older than"):
            self.run_signals(force=True)

    def test_bear_with_short_windows_counts_unscorable_tickers_instead_of_crashing(self):
        df = rows("^NSEI", self.days, 10000, -10)
        self.idx_store.upsert("NSEI", df)
        for regime_ in self.cfg["strategies"].values():
            regime_["SmallCap"].update(lookback=20, stock_trend_ma=20)
        self.small_cap({"FAST": (3.0, 1e7, FRIDAY)})
        for f in (self.market / "fresh").glob("*.csv"):
            f.unlink()
        self.store.upsert("FAST", rows("FAST", self.days[-40:], 100, 3.0))  # 40 rows: enough for MA 20, short of 70
        self.run_signals()
        t = self.targets()["buckets"]["SmallCap"]
        self.assertEqual((t["selected"], t["excluded"]["insufficientHistory"]), ([], 1))

    def test_missing_index_row_for_the_rebalance_date_is_a_gate(self):
        for f in (self.market / "indices/fresh").glob("*.csv"):
            f.unlink()
        self.index(self.days[:-1])
        with self.assertRaisesRegex(Gate, "no row"):
            self.run_signals()

    def test_too_many_tickers_missing_on_the_date_is_a_gate(self):
        (self.market / "fresh" / "MID.csv").unlink()
        self.store.upsert("MID", rows("MID", [d for d in self.days[-220:] if d <= "2026-09-24"], 100, 2.0))
        with self.assertRaisesRegex(Gate, "no row"):
            self.run_signals()

    def test_final_sunday_attempt_turns_a_gate_into_a_no_targets_failure(self):
        self.market_status(lastTradingDay="2026-09-24")
        report = self.run_signals(now=SUNDAY)
        self.assertIn(f"no targets for week of {FRIDAY}", report.error)
        with self.assertRaises(Gate):  # an earlier attempt just retries
            self.run_signals(now=datetime(2026, 9, 26, 21, 30, tzinfo=IST))

    def test_week_without_a_trading_day_does_nothing(self):
        self.set_calendar([f"2026-09-2{d}" for d in range(1, 6)])
        report = self.run_signals(now=datetime(2026, 9, 24, 21, 30, tzinfo=IST))
        self.assertTrue(report.quiet)


class RerunAndReplayTests(Env):
    def setUp(self):
        super().setUp()
        self.small_cap({"FAST": (3.0, 1e7, FRIDAY), "MID": (2.0, 1e7, FRIDAY)})

    def test_rerun_is_a_no_op_until_forced_then_supersedes(self):
        self.run_signals()
        path = self.analyst / "targets" / f"targets_{FRIDAY}.json"
        before = path.read_text(encoding="utf-8")
        self.assertTrue(self.run_signals().quiet)
        self.assertEqual(path.read_text(encoding="utf-8"), before)
        self.run_signals(force=True)
        self.assertEqual(len(list((self.analyst / "targets").glob("targets_*.superseded_*.json"))), 1)

    def test_replay_prints_and_writes_nothing(self):
        import contextlib
        import io

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            report = self.run_signals(now=datetime(2026, 11, 20, 12, 0, tzinfo=IST), as_of="2026-09-26")
        self.assertTrue(report.quiet)
        self.assertEqual(json.loads(out.getvalue())["rebalanceDate"], FRIDAY)
        self.assertFalse(self.analyst.exists())

    def test_replay_rejects_a_bad_or_too_early_date(self):
        with self.assertRaises(ValueError):
            self.run_signals(as_of="26-09-2026")
        with self.assertRaisesRegex(ValueError, "before the first rebalance"):
            self.run_signals(as_of="2000-01-01")

    def test_force_never_overwrites_a_previous_superseded_copy(self):
        for _ in range(3):
            self.run_signals(force=True)
        self.assertEqual(len(list((self.analyst / "targets").glob("targets_*.superseded_*.json"))), 2)

    def test_replay_gives_the_same_regime_as_the_live_run(self):
        import contextlib
        import io

        self.run_signals()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.run_signals(as_of=FRIDAY)
        self.assertEqual(json.loads(out.getvalue())["regime"], self.targets()["regime"])

    def test_old_target_files_are_deleted_by_retention(self):
        folder = self.analyst / "targets"
        folder.mkdir(parents=True)
        for name in ("targets_2020-01-03.json", "targets_2020-01-10.superseded_101010.json", "targets_2026-09-18.json"):
            (folder / name).write_text("{}")
        self.run_signals()
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["targets_2026-09-18.json", f"targets_{FRIDAY}.json"])


class HelperTests(unittest.TestCase):
    def test_execution_date_skips_weekends_and_holidays(self):
        from datetime import date

        from app.market.tradingcal import Calendar

        cal = Calendar.__new__(Calendar)
        cal.holidays, cal.special = {"2026-09-28"}, {"2026-09-26"}  # Monday holiday, Saturday special session
        self.assertEqual(signals.execution_date(cal, date(2026, 9, 25)), date(2026, 9, 29))

    def test_last_attempt_is_the_one_before_the_retry_window_closes(self):
        self.assertTrue(signals.is_last_attempt(SUNDAY, CFG))
        self.assertFalse(signals.is_last_attempt(datetime(2026, 9, 27, 20, 30, tzinfo=IST), CFG))
        self.assertFalse(signals.is_last_attempt(NOW, CFG))

    def test_tracked_symbol_strips_series_only_for_known_tickers(self):
        known = {"TATASTEEL", "BAJAJ-AUTO", "M&M"}
        self.assertEqual(common.tracked_symbol("TATASTEEL-EQ", known), "TATASTEEL")
        self.assertEqual(common.tracked_symbol("BAJAJ-AUTO-EQ", known), "BAJAJ-AUTO")
        self.assertEqual(common.tracked_symbol("BAJAJ-AUTO", known), "BAJAJ-AUTO")
        self.assertEqual(common.tracked_symbol("M&M-BE", known), "M&M")
        self.assertIsNone(common.tracked_symbol("UNKNOWN-EQ", known))


if __name__ == "__main__":
    unittest.main()
