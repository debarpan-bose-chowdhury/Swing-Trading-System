"""Ledger on a fake broker: fills, replayed book, journal, reconciliation, seeding, failures, housekeeping."""

import argparse
import copy
import json
import logging
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from app.analyst import common, journal, ledger
from app.analyst.broker import BrokerError, LoginFailed
from app.analyst.common import Report
from app.market import registry
from app.market.common import IST

LOG = logging.getLogger("test.ledger")
CFG = common.load_config()
MON, TUE, WED, THU, FRI = (f"2026-09-{d}" for d in (21, 22, 23, 24, 25))


def at(day: str) -> datetime:
    return datetime.fromisoformat(f"{day}T16:30:00+05:30").astimezone(IST)


def tb(symbol, side, qty, price, fid, time="10:00:00", **kw):
    return {"exchange": "NSE", "producttype": "DELIVERY", "tradingsymbol": symbol, "transactiontype": side, "fillsize": str(qty),
            "fillprice": str(price), "fillid": fid, "filltime": time, "orderid": f"O{fid}", **kw}


def hold(symbol, qty, avg, ltp=100.0, **kw):
    return {"tradingsymbol": symbol, "exchange": "NSE", "quantity": str(qty), "t1quantity": 0, "averageprice": avg, "ltp": ltp, "product": "DELIVERY", **kw}


class FakeBroker:
    def __init__(self, tradebook=(), holdings=(), positions=(), fail=(), login_error=None):
        self.data = {"tradebook": list(tradebook), "holdings": list(holdings), "positions": list(positions), "funds": {"net": "1"}}
        self.fail, self.login_error, self.calls, self.logged_out = set(fail), login_error, [], False

    def login(self):
        if self.login_error:
            raise LoginFailed(self.login_error)

    def logout(self):
        self.logged_out = True

    def __getattr__(self, name):
        if name not in self.data:
            raise AttributeError(name)

        def fetch():
            self.calls.append(name)
            if name in self.fail:
                raise BrokerError("AB9999", name)
            return self.data[name]

        return fetch


class Env(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        previous = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        self.cfg = copy.deepcopy(CFG)
        self.cfg["paths"] = {k: str(self.root / v) for k, v in {
            "analyst": "data/analyst", "market": "data/market", "upstreamStorage": "data/storage", "metadataConfig": "config/config.json",
            "calendar": "config/cal.json", "seed": "config/seed.csv", "logs": "data/logs"}.items()}
        (self.root / "config").mkdir()
        (self.root / "config/cal.json").write_text(json.dumps({"holidays": ["2026-10-02"], "specialSessions": []}))
        (self.root / "config/seed.csv").write_text("ticker,qty,entry_date,entry_price\n")
        (self.root / "data/market").mkdir(parents=True)
        reg = registry.load(self.root / "data/market/registry.csv")
        registry.refresh(reg, {s: "2026-09-01" for s in ("AAA", "BBB", "CCC", "BAJAJ-AUTO")})
        registry.save(reg, self.root / "data/market/registry.csv")
        self.analyst = self.root / "data/analyst"

    def seed(self, text):
        (self.root / "config/seed.csv").write_text("ticker,qty,entry_date,entry_price\n" + text)

    def day(self, day, broker, force=False):
        report, now = Report("ledger"), at(day)
        ledger.run(self.cfg, now, LOG, report, argparse.Namespace(force=force), broker=broker)
        if not report.quiet:
            common.write_status(self.cfg, report, now)
        return report

    def fills(self):
        return ledger.read_fills(self.analyst / "ledger/fills.csv")

    def book(self):
        return pd.read_csv(self.analyst / "ledger/book.csv", dtype={"ticker": str}).set_index("ticker")

    def journal(self):
        return journal.read(self.analyst / "trading_journal.csv")


class ReplayTests(unittest.TestCase):
    def fills(self, *rows):
        base = {"broker_symbol": "", "fill_time": "", "order_id": "", "run_id": "r", "kind": "FILL"}
        return pd.DataFrame([{**base, "fill_key": f"k{i}", **r} for i, r in enumerate(rows)])[ledger.FILL_COLS].astype({"qty": "int64", "price": "float64"})

    def test_average_cost_partial_sell_and_full_close(self):
        f = self.fills(
            dict(trade_date="2026-09-01", ticker="A", side="BUY", qty=10, price=100.0),
            dict(trade_date="2026-09-02", ticker="A", side="BUY", qty=10, price=120.0),
            dict(trade_date="2026-09-10", ticker="A", side="SELL", qty=5, price=130.0),
            dict(trade_date="2026-09-11", ticker="A", side="SELL", qty=15, price=140.0))
        book, sells, anomalies = ledger.replay(f.iloc[:3])
        self.assertEqual(book.iloc[0].to_dict(), {"ticker": "A", "qty": 15, "avg_price": 110.0, "entry_date": "2026-09-01", "entry_source": "FILLS"})
        book, sells, _ = ledger.replay(f)
        self.assertTrue(book.empty)
        self.assertEqual([(s["qty"], s["entry_price"], s["exit_price"], s["entry_date"], s["n"]) for s in sells],
                         [(5, 110.0, 130.0, "2026-09-01", 1), (15, 110.0, 140.0, "2026-09-01", 1)])

    def test_sells_of_one_day_become_one_row_at_the_weighted_price(self):
        f = self.fills(
            dict(trade_date="2026-09-01", ticker="A", side="BUY", qty=10, price=100.0),
            dict(trade_date="2026-09-05", ticker="A", side="SELL", qty=4, price=110.0, fill_time="2026-09-05T10:00:00+05:30"),
            dict(trade_date="2026-09-05", ticker="A", side="SELL", qty=6, price=120.0, fill_time="2026-09-05T11:00:00+05:30"))
        _, sells, _ = ledger.replay(f)
        self.assertEqual(len(sells), 1)
        self.assertEqual((sells[0]["qty"], sells[0]["exit_price"]), (10, 116.0))

    def test_buy_sorts_before_sell_when_there_is_no_fill_time(self):
        f = self.fills(dict(trade_date="2026-09-05", ticker="A", side="SELL", qty=5, price=110.0),
                       dict(trade_date="2026-09-05", ticker="A", side="BUY", qty=5, price=100.0))
        book, sells, anomalies = ledger.replay(f)
        self.assertEqual((len(sells), anomalies), (1, []))

    def test_sell_over_the_book_is_capped_and_flagged_and_unknown_sell_is_ignored(self):
        f = self.fills(dict(trade_date="2026-09-01", ticker="A", side="BUY", qty=5, price=100.0),
                       dict(trade_date="2026-09-02", ticker="A", side="SELL", qty=8, price=110.0),
                       dict(trade_date="2026-09-02", ticker="Z", side="SELL", qty=1, price=10.0))
        _, sells, anomalies = ledger.replay(f)
        self.assertEqual(sells[0]["qty"], 5)
        self.assertEqual(len(anomalies), 2)
        self.assertIn("exceeds", anomalies[0])

    def test_set_replaces_quantity_and_cost_but_keeps_the_entry(self):
        f = self.fills(dict(trade_date="2026-09-01", ticker="A", side="BUY", qty=10, price=100.0),
                       dict(trade_date="2026-09-08", ticker="A", side="SET", qty=20, price=50.0, kind="CORP_ACTION"))
        book, _, _ = ledger.replay(f)
        self.assertEqual(book.iloc[0].to_dict(), {"ticker": "A", "qty": 20, "avg_price": 50.0, "entry_date": "2026-09-01", "entry_source": "FILLS"})

    def test_second_closing_on_the_same_day_gets_the_next_trade_number(self):
        f = self.fills(dict(trade_date="2026-09-01", ticker="A", side="BUY", qty=10, price=100.0),
                       dict(trade_date="2026-09-05", ticker="A", side="SELL", qty=10, price=110.0),
                       dict(trade_date="2026-09-05", ticker="A", side="SELL", qty=1, price=90.0, kind="ESTIMATED"))
        book, sells, anomalies = ledger.replay(f)
        self.assertEqual([s["n"] for s in sells], [1])
        self.assertEqual(len(anomalies), 1)


class LedgerRunTests(Env):
    def test_first_run_seeds_buys_and_reconciles_with_the_broker(self):
        self.seed("AAA,10,2026-08-01,100\n")
        b = FakeBroker(tradebook=[tb("BBB-EQ", "BUY", 5, 200, "1")], holdings=[hold("AAA-EQ", 10, 100), hold("BBB-EQ", 5, 200)])
        r = self.day(MON, b)
        book = self.book()
        self.assertEqual(book.loc["AAA"].to_dict(), {"qty": 10, "avg_price": 100.0, "entry_date": "2026-08-01", "entry_source": "SEED", "last_reconciled": MON})
        self.assertEqual((book.loc["BBB", "entry_date"], book.loc["BBB", "entry_source"]), (MON, "FILLS"))
        self.assertTrue((self.analyst / "seed.done").exists())
        self.assertEqual(r.status(), "ok")
        self.assertEqual(sorted(p.name for p in (self.analyst / "snapshots").iterdir()), [f"{n}_{MON}.json" for n in ("funds", "holdings", "positions", "tradebook")])
        snap = json.loads((self.analyst / f"snapshots/holdings_{MON}.json").read_text())
        self.assertEqual((snap["endpoint"], len(snap["rows"])), ("holdings", 2))
        self.assertTrue(b.logged_out)
        self.assertEqual(b.calls, ["tradebook", "positions", "holdings", "funds"])
        self.assertEqual(r.block["fillsRecorded"], 2)

    def test_holdings_missing_from_the_seed_file_enter_the_book_as_unknown(self):
        r = self.day(MON, FakeBroker(holdings=[hold("CCC-EQ", 7, 50.0)]))
        row = self.book().loc["CCC"]
        self.assertEqual((row.qty, row.avg_price, row.entry_date, row.entry_source), (7, 50.0, "UNKNOWN", "UNKNOWN"))
        self.assertTrue(any("UNSEEDED CCC" in line for line in r.lines))

    def test_a_sale_writes_a_journal_row_with_pl_and_charges(self):
        self.seed("AAA,10,2026-08-01,100\n")
        self.day(MON, FakeBroker(holdings=[hold("AAA-EQ", 10, 100)]))
        r = self.day(TUE, FakeBroker(tradebook=[tb("AAA-EQ", "SELL", 4, 110, "2")], holdings=[hold("AAA-EQ", 6, 100)]))
        j = self.journal().iloc[0]
        self.assertEqual((j.trade_id, j.qty, j.entry_date, j.entry_price, j.exit_date, j.exit_price), (f"AAA-{TUE}-1", "4", "2026-08-01", "100.0000", TUE, "110.0000"))
        self.assertEqual((j.pl, j.pl_pct, j.source, j.entry_source), ("40.00", "10.00", "FILLS", "SEED"))
        self.assertGreater(float(j.est_charges), 0)
        self.assertEqual(float(j.net_pl), round(40 - float(j.est_charges), 2))
        self.assertEqual(self.book().loc["AAA"].to_dict()["qty"], 6)
        self.assertEqual(r.block["journalRowsAdded"], 1)

    def test_rerunning_the_same_day_changes_nothing(self):
        self.seed("AAA,10,2026-08-01,100\n")
        self.day(MON, FakeBroker(holdings=[hold("AAA-EQ", 10, 100)]))
        b = lambda: FakeBroker(tradebook=[tb("AAA-EQ", "SELL", 4, 110, "2")], holdings=[hold("AAA-EQ", 6, 100)])  # noqa: E731
        self.day(TUE, b())
        before = (self.fills().copy(), self.journal().copy(), self.book().copy())
        r = self.day(TUE, b(), force=True)
        self.assertEqual((r.block["fillsRecorded"], r.block["journalRowsAdded"]), (0, 0))
        for old, new in zip(before, (self.fills(), self.journal(), self.book())):
            pd.testing.assert_frame_equal(old.reset_index(drop=True), new.reset_index(drop=True), check_dtype=False)

    def test_done_today_and_non_trading_days_do_nothing(self):
        self.day(MON, FakeBroker())
        b = FakeBroker()
        self.assertTrue(self.day(MON, b).quiet)
        self.assertTrue(self.day("2026-10-02", b).quiet)  # holiday
        self.assertTrue(self.day("2026-09-26", b).quiet)  # Saturday
        self.assertEqual(b.calls, [])
        self.assertFalse(self.day("2026-09-26", FakeBroker(), force=True).quiet)

    def test_untracked_fills_and_holdings_are_reported_not_booked(self):
        self.cfg["capital"]["ignoreSymbols"] = ["CCC"]
        b = FakeBroker(tradebook=[tb("ZZZ-EQ", "BUY", 1, 10, "1"), tb("CCC-EQ", "BUY", 1, 10, "2"), tb("AAA-EQ", "BUY", 1, 10, "3", exchange="BSE")],
                       holdings=[hold("ZZZ-EQ", 1, 10), hold("CCC-EQ", 1, 10)])
        r = self.day(MON, b)
        self.assertTrue(self.book().empty)
        self.assertEqual(r.block["untracked"], ["CCC-EQ", "ZZZ-EQ"])
        self.assertEqual(self.fills().empty, True)

    def test_series_suffix_matching_handles_hyphenated_symbols(self):
        self.day(MON, FakeBroker(tradebook=[tb("BAJAJ-AUTO-EQ", "BUY", 2, 9000, "1")], holdings=[hold("BAJAJ-AUTO-EQ", 2, 9000)]))
        self.assertEqual(self.book().loc["BAJAJ-AUTO", "qty"], 2)


class ReconcileTests(Env):
    def start(self):
        self.seed("AAA,10,2026-08-01,100\n")
        self.day(MON, FakeBroker(holdings=[hold("AAA-EQ", 10, 100, ltp=104.0)]))

    def test_split_within_cost_tolerance_is_a_corporate_action_without_a_journal_row(self):
        self.start()
        r = self.day(TUE, FakeBroker(holdings=[hold("AAA-EQ", 20, 50.0)]))
        row = self.book().loc["AAA"]
        self.assertEqual((row.qty, row.avg_price, row.entry_date), (20, 50.0, "2026-08-01"))
        self.assertTrue(self.journal().empty)
        self.assertTrue(any(line.startswith("CORP_ACTION AAA") for line in r.lines))

    def test_missed_buy_takes_the_broker_average_and_keeps_the_entry_date(self):
        self.start()
        r = self.day(TUE, FakeBroker(holdings=[hold("AAA-EQ", 15, 108.0)]))
        row = self.book().loc["AAA"]
        self.assertEqual((row.qty, row.avg_price, row.entry_date, row.entry_source), (15, 108.0, "2026-08-01", "BROKER_AVG"))
        self.assertTrue(any(line.startswith("MISSED_BUY AAA") for line in r.lines))

    def test_missed_sell_writes_an_estimated_row_at_the_last_known_ltp(self):
        self.start()
        r = self.day(TUE, FakeBroker(holdings=[hold("AAA-EQ", 4, 100.0, ltp=111.0)]))
        j = self.journal().iloc[0]
        self.assertEqual((j.qty, j.exit_price, j.exit_date, j.source), ("6", "104.0000", TUE, "ESTIMATED"))  # Monday's snapshot ltp
        self.assertEqual(self.book().loc["AAA", "qty"], 4)
        self.assertTrue(any("ESTIMATED journal rows awaiting" in line for line in r.lines))

    def test_ticker_gone_from_holdings_is_fully_sold(self):
        self.start()
        self.day(TUE, FakeBroker(holdings=[]))
        self.assertTrue(self.book().empty)
        self.assertEqual(self.journal().iloc[0].qty, "10")

    def test_positions_cover_shares_bought_today_that_holdings_do_not_show_yet(self):
        pos = [{"tradingsymbol": "BBB-EQ", "producttype": "DELIVERY", "netqty": "3", "avgnetprice": "50"}]
        self.day(MON, FakeBroker(tradebook=[tb("BBB-EQ", "BUY", 3, 50, "1")], positions=pos, holdings=[]))
        self.assertEqual(self.book().loc["BBB", "qty"], 3)
        self.assertTrue(self.journal().empty)

    def test_observed_quantity_fields_are_configurable_for_the_t1_rule(self):
        self.cfg["ledger"]["observedQtyFields"] = ["quantity", "t1quantity"]
        h = hold("BBB-EQ", 5, 50.0)
        h["t1quantity"] = 3
        self.day(MON, FakeBroker(holdings=[h]))
        self.assertEqual(self.book().loc["BBB", "qty"], 8)

    def test_editing_an_estimated_row_promotes_it_and_recomputes_without_touching_your_columns(self):
        self.start()
        self.day(TUE, FakeBroker(holdings=[hold("AAA-EQ", 4, 100.0)]))
        path = self.analyst / "trading_journal.csv"
        df = pd.read_csv(path, dtype=str, keep_default_na=False)
        df.loc[0, ["exit_price", "notes", "tags"]] = ["120", "contract note 1", "swing"]
        df.to_csv(path, index=False)
        r = self.day(WED, FakeBroker(holdings=[hold("AAA-EQ", 4, 100.0)]))
        j = self.journal().iloc[0]
        self.assertEqual((j.source, j.pl, j.exit_price, j.notes, j.tags), ("MANUAL_VERIFIED", "120.00", "120", "contract note 1", "swing"))
        self.assertEqual(j.auto_hash, journal.auto_hash(j))
        self.assertTrue(any("promoted" in line for line in r.lines))
        self.day(THU, FakeBroker(holdings=[hold("AAA-EQ", 4, 100.0)]))
        self.assertEqual(self.journal().iloc[0].pl, "120.00")  # stable once promoted


class FailureTests(Env):
    def test_login_failure_fails_the_run_and_still_logs_out(self):
        b = FakeBroker(login_error="broker login failed: AB1050")
        r = self.day(MON, b)
        self.assertEqual((r.status(), r.error), ("failed", "broker login failed: AB1050"))
        self.assertFalse((self.analyst / "ledger").exists())

    def test_failed_tradebook_is_partial_and_the_next_run_reconciles_by_diff(self):
        self.seed("AAA,10,2026-08-01,100\n")
        self.day(MON, FakeBroker(holdings=[hold("AAA-EQ", 10, 100)]))
        r = self.day(TUE, FakeBroker(tradebook=[tb("AAA-EQ", "SELL", 10, 110, "9")], holdings=[], fail={"tradebook"}), force=True)
        self.assertEqual(r.status(), "partial")
        self.assertIn("tradebook (AB9999)", r.lines[0])
        self.assertEqual(self.journal().iloc[0].source, "ESTIMATED")  # holdings alone showed the sale
        self.assertFalse((self.analyst / f"snapshots/tradebook_{TUE}.json").exists())

    def test_failed_holdings_records_fills_but_skips_reconciliation_and_seed_done(self):
        self.seed("AAA,10,2026-08-01,100\n")
        r = self.day(MON, FakeBroker(tradebook=[tb("BBB-EQ", "BUY", 2, 50, "1")], fail={"holdings"}))
        self.assertEqual(r.status(), "partial")
        self.assertEqual(sorted(self.book().index), ["AAA", "BBB"])
        self.assertFalse((self.analyst / "seed.done").exists())
        self.assertTrue(any("reconciliation skipped" in line for line in r.lines))
        self.day(TUE, FakeBroker(holdings=[hold("AAA-EQ", 10, 100), hold("BBB-EQ", 2, 50)]))
        self.assertTrue((self.analyst / "seed.done").exists())
        self.assertEqual(len(self.fills()), 2)  # seed row not duplicated

    def test_unmapped_tradebook_field_fails_fast_naming_it_and_keeps_the_raw_snapshot(self):
        row = tb("AAA-EQ", "BUY", 1, 10, "1")
        del row["fillprice"]
        r = self.day(MON, FakeBroker(tradebook=[row]))
        self.assertEqual(r.status(), "failed")
        self.assertIn("fillprice", r.error)
        self.assertTrue((self.analyst / f"snapshots/tradebook_{MON}.json").exists())

    def test_journal_locked_by_excel_catches_up_on_the_next_run(self):
        self.seed("AAA,10,2026-08-01,100\n")
        self.day(MON, FakeBroker(holdings=[hold("AAA-EQ", 10, 100)]))
        sale = lambda: FakeBroker(tradebook=[tb("AAA-EQ", "SELL", 10, 110, "2")], holdings=[])  # noqa: E731
        with patch("app.analyst.journal.write_csv", side_effect=PermissionError("locked")):
            r = self.day(TUE, sale())
        self.assertEqual(r.status(), "partial")
        self.assertTrue(self.journal().empty)
        r = self.day(WED, FakeBroker(holdings=[]))
        self.assertEqual(r.block["journalRowsAdded"], 1)
        self.assertEqual(self.journal().iloc[0].trade_id, f"AAA-{TUE}-1")

    def test_book_and_journal_rebuild_from_fills_after_a_crash_between_writes(self):
        self.seed("AAA,10,2026-08-01,100\n")
        self.day(MON, FakeBroker(holdings=[hold("AAA-EQ", 10, 100)]))
        self.day(TUE, FakeBroker(tradebook=[tb("AAA-EQ", "SELL", 4, 110, "2")], holdings=[hold("AAA-EQ", 6, 100)]))
        want = (self.book().copy(), self.journal().copy())
        (self.analyst / "ledger/book.csv").unlink()
        (self.analyst / "trading_journal.csv").unlink()
        r = self.day(WED, FakeBroker(holdings=[hold("AAA-EQ", 6, 100)]))
        pd.testing.assert_frame_equal(self.book().drop(columns="last_reconciled"), want[0].drop(columns="last_reconciled"), check_dtype=False)
        self.assertEqual(self.journal().trade_id.tolist(), want[1].trade_id.tolist())
        self.assertFalse(any("MISSED" in line for line in r.lines))

    def test_sell_larger_than_the_book_is_capped_and_flagged_in_the_digest(self):
        self.seed("AAA,5,2026-08-01,100\n")
        self.day(MON, FakeBroker(holdings=[hold("AAA-EQ", 5, 100)]))
        r = self.day(TUE, FakeBroker(tradebook=[tb("AAA-EQ", "SELL", 8, 110, "2")], holdings=[]))
        self.assertTrue(any(line.startswith("ANOMALY") and "capped" in line for line in r.lines))
        self.assertEqual(self.journal().iloc[0].qty, "5")
        self.assertEqual(len(self.fills()[self.fills().side == "SELL"]), 1)  # the fill is still stored


class HousekeepingTests(Env):
    def test_missed_trading_days_are_listed_from_the_last_good_run(self):
        self.day(MON, FakeBroker())
        r = self.day(THU, FakeBroker())
        self.assertEqual(r.block["missedTradingDays"], [TUE, WED])
        self.assertTrue(any("no snapshot" in line for line in r.lines))
        self.assertEqual(self.day(FRI, FakeBroker()).block["missedTradingDays"], [])

    def test_backup_copies_the_three_files_and_expires_old_folders_and_snapshots(self):
        self.seed("AAA,10,2026-08-01,100\n")
        self.day(MON, FakeBroker(tradebook=[tb("AAA-EQ", "SELL", 10, 110, "2")], holdings=[]))
        today = self.analyst / "backup" / MON
        self.assertEqual(sorted(p.name for p in today.iterdir()), ["book.csv", "fills.csv", "trading_journal.csv"])
        (self.analyst / "backup/2026-01-01").mkdir()
        snaps = self.analyst / "snapshots"
        for name in ("holdings_2026-01-01.json", "holdings_2026-01-02.json", "funds_2026-01-01.json"):
            (snaps / name).write_text("{}")
        self.day(TUE, FakeBroker())
        self.assertFalse((self.analyst / "backup/2026-01-01").exists())
        self.assertTrue((self.analyst / "backup" / MON).exists())  # inside the 30 days
        left = sorted(p.name for p in snaps.iterdir())
        self.assertNotIn("holdings_2026-01-01.json", left)
        self.assertNotIn("holdings_2026-01-02.json", left)  # old, and not the newest of its type
        self.assertIn(f"holdings_{TUE}.json", left)

    def test_the_newest_snapshot_of_a_type_is_kept_even_when_old(self):
        snaps = self.analyst / "snapshots"
        snaps.mkdir(parents=True)
        (snaps / "funds_2026-01-01.json").write_text("{}")
        ledger.housekeeping(self.analyst, datetime(2026, 9, 25).date(), self.cfg)
        self.assertTrue((snaps / "funds_2026-01-01.json").exists())


class SeedAndParseTests(Env):
    def test_seed_ignores_untracked_and_blank_rows(self):
        self.seed("AAA,10,2026-08-01,100\nNOPE,1,2026-08-01,1\n,,,\n")
        fills, skipped = ledger.seed_fills(self.root / "config/seed.csv", {"AAA"}, set(), "r")
        self.assertEqual(([f["ticker"] for f in fills], skipped), (["AAA"], ["NOPE"]))
        self.assertEqual(ledger.seed_fills(self.root / "config/missing.csv", {"AAA"}, set(), "r"), ([], []))

    def test_fill_key_falls_back_to_a_hash_and_fill_time_gets_the_offset(self):
        row = tb("AAA-EQ", "BUY", 1, 10, "")
        fills, _, skipped = ledger.parse_tradebook([row, tb("AAA-EQ", "BUY", 1, 10, "7", producttype="INTRADAY")], MON, "r", {"AAA"}, set())
        self.assertEqual((len(fills), skipped), (1, 1))
        self.assertRegex(fills[0]["fill_key"], rf"^{MON}-[0-9a-f]{{40}}$")
        self.assertEqual(fills[0]["fill_time"], f"{MON}T10:00:00+05:30")
        again, _, _ = ledger.parse_tradebook([row], MON, "r", {"AAA"}, set())
        self.assertEqual(fills[0]["fill_key"], again[0]["fill_key"])


if __name__ == "__main__":
    unittest.main()
