"""Store, fetcher and ingest."""

import logging
from unittest.mock import patch

import pandas as pd
import pyarrow.parquet as pq
import yfinance as yf

from app.market import ingest
from app.market.common import COLS, Report
from app.market.fetcher import Blocked, Fetcher, _Capture, tidy
from app.market.ingest import Series, backfill, build_series, fetch_valid
from app.market.store import Store
from app.market.tradingcal import Calendar
from tests.market_helpers import CUTOFF, LOG, Env, bars, weekdays

_REAL_YF = Fetcher._yf  # captured before Env patches it
OLD = ["2025-01-02", "2025-01-03"]
NEW = ["2026-09-28", "2026-09-29"]


class StoreTests(Env):
    def test_upsert_creates_sorts_dedupes_and_incoming_wins(self):
        self.stored("A", ["2026-09-29", "2026-09-28"], close=100)
        self.stored("A", ["2026-09-29"], close=200)
        df = self.store.read_fresh("A")
        self.assertEqual(list(df.Date), ["2026-09-28", "2026-09-29"])
        self.assertEqual(list(df.Close), [100.0, 200.0])
        self.assertEqual(list(df.columns), COLS)

    def test_special_ticker_names_are_kept(self):
        self.stored("M&M", NEW)
        self.assertTrue((self.market / "fresh" / "M&M.csv").exists())
        self.assertEqual(list(self.store.read_fresh("M&M").Ticker.unique()), ["M&M"])

    def test_last_date_none_fresh_and_archive_fallback(self):
        self.assertIsNone(self.store.last_date("A"))
        self.stored("A", NEW)
        self.assertEqual(self.store.last_date("A"), "2026-09-29")
        self.store.rebuild("B", bars("B", OLD))
        self.assertEqual(self.store.last_date("B"), "2025-01-03")

    def test_rebuild_splits_at_cutoff(self):
        self.store.rebuild("A", bars("A", OLD + NEW))
        self.assertEqual(list(self.store.read_fresh("A").Date), NEW)
        self.assertEqual(list(self.store.read_archive("A").Date), OLD)
        self.assertTrue((self.market / "archive" / "A" / "Compressed_2025-01-02_2025-01-03.parquet").exists())
        self.assertFalse((self.market / ".tmp" / "A").exists())

    def test_rebuild_row_exactly_on_cutoff_stays_fresh(self):
        self.store.rebuild("A", bars("A", ["2025-09-26", CUTOFF]))
        self.assertEqual(list(self.store.read_fresh("A").Date), [CUTOFF])

    def test_rebuild_replaces_history_and_old_partitions(self):
        self.store.rebuild("A", bars("A", OLD + NEW, close=100))
        self.store.rebuild("A", bars("A", ["2025-02-03"] + NEW, close=50))
        self.assertEqual([p.name for p in (self.market / "archive" / "A").glob("*")], ["Compressed_2025-02-03_2025-02-03.parquet"])
        self.assertEqual(set(self.store.read_fresh("A").Close), {50.0})

    def test_rebuild_with_no_old_rows_removes_stale_archive(self):
        self.store.rebuild("A", bars("A", OLD + NEW))
        self.store.rebuild("A", bars("A", NEW))
        self.assertFalse((self.market / "archive" / "A").exists())

    def test_parquet_is_zstd_with_date_type(self):
        self.store.rebuild("A", bars("A", OLD + NEW))
        f = next((self.market / "archive" / "A").glob("*.parquet"))
        meta = pq.ParquetFile(f)
        self.assertEqual(meta.metadata.row_group(0).column(0).compression, "ZSTD")
        self.assertEqual(str(meta.schema_arrow.field("Date").type), "date32[day]")

    def test_read_archive_spans_partitions(self):
        self.store.rebuild("A", bars("A", OLD + NEW))
        self.store.upsert("A", bars("A", ["2025-03-03"]))
        self.store.archive_aged("A")
        self.assertEqual(list(self.store.read_archive("A").Date), ["2025-01-02", "2025-01-03", "2025-03-03"])

    def test_archive_aged_moves_only_old_rows(self):
        self.store.upsert("A", bars("A", OLD + NEW))
        self.assertEqual(self.store.archive_aged("A"), 2)
        self.assertEqual(list(self.store.read_fresh("A").Date), NEW)
        self.assertEqual(list(self.store.read_archive("A").Date), OLD)

    def test_archive_aged_nothing_to_do(self):
        self.stored("A", NEW)
        self.assertEqual(self.store.archive_aged("A"), 0)
        self.assertEqual(self.store.archive_aged("missing"), 0)
        self.assertFalse((self.market / "archive").exists())

    def test_archive_aged_all_rows_old_leaves_empty_csv(self):
        self.store.upsert("A", bars("A", OLD))
        self.store.archive_aged("A")
        self.assertTrue(self.store.read_fresh("A").empty)
        self.assertEqual(self.store.last_date("A"), "2025-01-03")

    def test_crash_between_parquet_and_csv_leaves_duplicates_then_heals(self):
        self.store.upsert("A", bars("A", OLD + NEW))
        with patch("app.market.store.write_csv", side_effect=OSError("crash")), self.assertRaises(OSError):
            self.store.archive_aged("A")
        self.assertEqual(len(self.store.read_fresh("A")), 4)  # nothing lost, duplicated across tiers
        self.assertEqual(len(self.store.read_archive("A")), 2)
        self.store.archive_aged("A")
        self.assertEqual(list(self.store.read_fresh("A").Date), NEW)
        self.assertEqual(len(self.store.read_archive("A")), 2)
        self.assertEqual(len(list((self.market / "archive" / "A").glob("*.parquet"))), 1)

    def test_verification_failure_keeps_csv_and_no_partition(self):
        self.store.upsert("A", bars("A", OLD + NEW))
        real = pd.read_parquet
        with patch("app.market.store.pd.read_parquet", side_effect=lambda p: real(p).head(1)), self.assertRaises(ValueError):
            self.store.archive_aged("A")
        self.assertEqual(len(self.store.read_fresh("A")), 4)
        self.assertEqual(list((self.market / "archive" / "A").glob("*")), [])

    def test_existing_partition_is_never_overwritten(self):
        self.store.rebuild("A", bars("A", OLD + NEW))
        self.store.upsert("A", bars("A", ["2025-01-02"], close=7))
        # 2025-01-02 is already archived so it is dropped, not re-written
        self.assertEqual(self.store.archive_aged("A"), 1)
        self.assertEqual(len(list((self.market / "archive" / "A").glob("*.parquet"))), 1)
        self.assertEqual(set(self.store.read_archive("A").Close), {100.0})


class TidyTests(Env):
    def raw(self, **kw) -> pd.DataFrame:
        idx = pd.to_datetime(["2026-09-28", "2026-09-29"])
        fields = ["Open", "High", "Low", "Close", "Adj Close", "Volume", "Dividends", "Stock Splits"]
        cols = pd.MultiIndex.from_product([["AAA.NS", "BBB.NS"], fields])
        df = pd.DataFrame(1.0, index=idx, columns=cols)
        for k, v in kw.items():
            df.loc[idx[0], ("AAA.NS", k)] = v
        return df

    def test_multiindex_to_long_schema(self):
        out = tidy(self.raw(), ["AAA.NS", "BBB.NS"])
        self.assertEqual(len(out), 4)
        self.assertEqual(list(out.columns), COLS + ["Dividends", "Splits"])
        self.assertEqual(set(out.Ticker), {"AAA.NS", "BBB.NS"})
        self.assertEqual(list(out.Date[:2]), ["2026-09-28", "2026-09-29"])

    def test_all_nan_rows_are_dropped_but_partial_rows_kept(self):
        raw = self.raw()
        raw.loc[raw.index[0], "BBB.NS"] = float("nan")
        raw.loc[raw.index[1], ("BBB.NS", "Close")] = float("nan")
        out = tidy(raw, ["AAA.NS", "BBB.NS"])
        self.assertEqual(len(out[out.Ticker == "BBB.NS"]), 1)
        self.assertTrue(out[out.Ticker == "BBB.NS"].Close.isna().all())

    def test_symbol_missing_from_response_and_empty_response(self):
        self.assertEqual(set(tidy(self.raw(), ["CCC.NS"]).Ticker), set())
        self.assertTrue(tidy(None, ["A"]).empty)
        self.assertTrue(tidy(pd.DataFrame(), ["A"]).empty)

    def test_single_level_columns_are_wrapped(self):
        raw = self.raw()["AAA.NS"]
        self.assertEqual(set(tidy(raw, ["AAA.NS"]).Ticker), {"AAA.NS"})

    def test_missing_actions_columns_default_to_zero(self):
        raw = self.raw().drop(columns=["Dividends", "Stock Splits"], level=1)
        out = tidy(raw, ["AAA.NS"])
        self.assertEqual((out.Dividends.sum(), out.Splits.sum()), (0, 0))


class YfBoundaryTests(Env):
    """The real Fetcher._yf against a mocked yf.download."""

    def test_rate_limit_exception_becomes_blocked(self):
        from app.market import fetcher as mod
        raw = _REAL_YF
        with patch.object(mod.yf, "download", side_effect=yf.exceptions.YFRateLimitError()), self.assertRaises(Blocked):
            raw(self.fetcher(), ["A.NS"])

    def test_rate_limit_hidden_in_yfinance_log_becomes_blocked(self):
        from app.market import fetcher as mod

        def download(*a, **k):
            logging.getLogger("yfinance").error("['A.NS']: YFRateLimitError('Too Many Requests')")
            return pd.DataFrame()

        with patch.object(mod.yf, "download", side_effect=download), self.assertRaises(Blocked):
            _REAL_YF(self.fetcher(), ["A.NS"])

    def test_other_errors_propagate_and_handler_is_removed(self):
        from app.market import fetcher as mod
        before = list(logging.getLogger("yfinance").handlers)
        with patch.object(mod.yf, "download", side_effect=ValueError("boom")), self.assertRaises(ValueError):
            _REAL_YF(self.fetcher(), ["A.NS"])
        self.assertEqual(logging.getLogger("yfinance").handlers, before)

    def test_normal_call_uses_raw_prices_with_actions(self):
        from app.market import fetcher as mod
        with patch.object(mod.yf, "download", return_value=pd.DataFrame()) as dl:
            out = _REAL_YF(self.fetcher(), ["A.NS"], period="max")
        kw = dl.call_args.kwargs
        self.assertFalse(kw["auto_adjust"])
        self.assertTrue(kw["actions"])
        self.assertEqual((kw["interval"], kw["period"]), ("1d", "max"))
        self.assertTrue(out.empty)

    def test_capture_handler(self):
        h = _Capture()
        h.emit(logging.LogRecord("yfinance", 40, "", 0, "HTTP Error 429: Too Many Requests", None, None))
        self.assertTrue(h.blocked)
        h2 = _Capture()
        h2.emit(logging.LogRecord("yfinance", 40, "", 0, "$X.NS: possibly delisted", None, None))
        self.assertFalse(h2.blocked)


class FetcherTests(Env):
    def test_fetch_success_counts_budget(self):
        self.yahoo_has("A.NS", NEW)
        fx = self.fetcher()
        self.assertEqual(len(fx.fetch(["A.NS"])), 2)
        self.assertEqual(fx.used, 1)

    def test_retries_with_backoff_then_succeeds(self):
        self.yahoo_has("A.NS", NEW)
        self.yahoo.script = [OSError("net"), OSError("net")]
        out = self.fetcher().fetch(["A.NS"])
        self.assertEqual(len(out), 2)
        self.assertEqual(len(self.yahoo.calls), 3)
        waits = [w for w in self.sleeps if w > 0]
        self.assertTrue(2 <= waits[0] < 3 and 4 <= waits[1] < 5)

    def test_gives_up_after_three_retries(self):
        self.yahoo.script = [OSError("net")] * 10
        self.assertIsNone(self.fetcher().fetch(["A.NS"]))
        self.assertEqual(len(self.yahoo.calls), 4)  # 1 try + 3 retries
        self.assertTrue(8 <= [w for w in self.sleeps if w > 0][2] < 9)

    def test_block_pauses_an_hour_then_resumes(self):
        self.yahoo_has("A.NS", NEW)
        self.yahoo.script = [Blocked()]
        fx = self.fetcher()
        self.assertEqual(len(fx.fetch(["A.NS"])), 2)
        self.assertIn(3600, self.sleeps)
        self.assertEqual(fx.pauses, 1)
        self.assertFalse(fx.exhausted)

    def test_block_pause_is_not_counted_as_a_retry(self):
        self.yahoo_has("A.NS", NEW)
        self.yahoo.script = [Blocked(), OSError(), OSError(), OSError()]
        self.assertEqual(len(self.fetcher().fetch(["A.NS"])), 2)

    def test_pauses_exhausted_defers_everything_after(self):
        self.yahoo.script = [Blocked()] * 4
        fx = self.fetcher()
        self.assertIsNone(fx.fetch(["A.NS"]))
        self.assertTrue(fx.exhausted)
        self.assertEqual(fx.pauses, 3)
        calls = len(self.yahoo.calls)
        self.assertIsNone(fx.fetch(["B.NS"]))
        self.assertEqual(len(self.yahoo.calls), calls)  # no request once exhausted

    def test_gap_between_calls(self):
        self.cfg["fetch"]["batchGapSeconds"] = 15
        self.yahoo_has("A.NS", NEW)
        fx = self.fetcher()
        fx.fetch(["A.NS"])
        self.sleeps.clear()
        fx.fetch(["A.NS"])
        self.assertTrue(0 < self.sleeps[0] <= 15)

    def test_budget_exhaustion_waits_for_next_hour(self):
        self.cfg["fetch"]["hourlyRequestBudget"] = 3
        self.yahoo_has("A.NS", NEW)
        fx = self.fetcher()
        fx.fetch(["A.NS", "B.NS"])
        self.assertFalse(any(s > 1000 for s in self.sleeps))
        fx.fetch(["A.NS", "B.NS"])
        self.assertTrue(any(3000 < s <= 3600 for s in self.sleeps))
        self.assertEqual(fx.used, 2)

    def test_batches_chunk_and_report_failures_as_none(self):
        self.yahoo.script = [None, *[OSError()] * 4]
        out = list(self.fetcher().batches(["A", "B", "C"], 2))
        self.assertEqual([c for c, _ in out], [["A", "B"], ["C"]])
        self.assertIsNotNone(out[0][1])
        self.assertIsNone(out[1][1])


class IngestTests(Env):
    def setUp(self) -> None:
        super().setUp()
        self.cal = Calendar(self.cfg["paths"]["calendar"])
        self.s = Series("A", "A.NS", "A", self.store)
        self.idx = Series("^NSEI", "^NSEI", "NSEI", self.idx_store)

    def fetch(self, series, last="2026-09-29", **kw):
        return list(fetch_valid(self.fetcher(), self.cal, self.cfg, self.report, series, last, **kw))

    def test_build_series_equities_and_indices(self):
        self.set_indices(["^NSEI", "^BSESN"])
        series = build_series(self.cfg, ["AAA", "M&M"], CUTOFF)
        self.assertEqual([(s.ticker, s.yahoo, s.key) for s in series],
                         [("AAA", "AAA.NS", "AAA"), ("M&M", "M&M.NS", "M&M"), ("^NSEI", "^NSEI", "NSEI"), ("^BSESN", "^BSESN", "BSESN")])
        self.assertIs(series[0].store.root, series[1].store.root)
        self.assertEqual(series[2].store.root, self.market / "indices")

    def test_valid_rows_and_ticker_renamed(self):
        self.yahoo_has("A.NS", NEW)
        (s, valid, rej, raw), = self.fetch([self.s], period="max")
        self.assertEqual(set(valid.Ticker), {"A"})
        self.assertTrue(rej.empty)

    def test_rows_after_last_final_session_are_clipped(self):
        self.yahoo_has("A.NS", NEW)
        (_, valid, _, raw), = self.fetch([self.s], last="2026-09-28")
        self.assertEqual(list(valid.Date), ["2026-09-28"])
        self.assertEqual(len(raw), 1)

    def test_null_row_is_refetched_and_replaced(self):
        df = self.yahoo_has("A.NS", NEW)
        bad = df.copy()
        bad.loc[1, "Close"] = None
        self.yahoo.script = [bad]
        (_, valid, rej, _), = self.fetch([self.s])
        self.assertEqual(list(valid.Date), NEW)
        self.assertTrue(rej.empty)
        self.assertEqual(len(self.yahoo.calls), 2)
        self.assertEqual(self.yahoo.calls[1][1]["start"], "2026-09-29")

    def test_null_persisting_after_two_passes_is_rejected(self):
        df = self.yahoo_has("A.NS", NEW)
        bad = df.copy()
        bad.loc[1, "Close"] = None
        self.yahoo.script = [bad, bad.iloc[[1]], bad.iloc[[1]]]
        (_, valid, rej, _), = self.fetch([self.s])
        self.assertEqual(list(valid.Date), ["2026-09-28"])
        self.assertEqual(list(rej.Reason), ["NULL_VALUE"])
        self.assertEqual(len(self.yahoo.calls), 3)  # original + 2 refetch passes only

    def test_null_row_missing_from_refetch_stays_rejected(self):
        df = self.yahoo_has("A.NS", NEW)
        bad = df.copy()
        bad.loc[1, "Close"] = None
        self.yahoo.script = [bad, pd.DataFrame(columns=bad.columns), pd.DataFrame(columns=bad.columns)]
        (_, valid, rej, _), = self.fetch([self.s])
        self.assertEqual(list(rej.Reason), ["NULL_VALUE"])

    def test_failed_refetch_leaves_row_rejected(self):
        df = self.yahoo_has("A.NS", NEW)
        bad = df.copy()
        bad.loc[1, "Close"] = None
        self.yahoo.script = [bad] + [OSError()] * 4
        (_, valid, rej, _), = self.fetch([self.s])
        self.assertEqual(list(rej.Reason), ["NULL_VALUE"])

    def test_rejects_are_reported(self):
        self.yahoo_has("A.NS", NEW, High=1.0)
        self.fetch([self.s])
        self.assertEqual(len(self.report.rejected()), 2)

    def test_failed_fetch_is_listed(self):
        self.yahoo.script = [OSError()] * 4
        self.assertEqual(self.fetch([self.s]), [])
        self.assertEqual(self.report.failed, {"A": "fetch failed after retries"})

    def test_deferred_after_block_pauses_is_listed(self):
        self.yahoo.script = [Blocked()] * 4
        self.fetch([self.s])
        self.assertIn("deferred", self.report.failed["A"])

    def test_indices_are_fetched_one_at_a_time_equities_in_batches(self):
        for sym in ("A.NS", "B.NS", "C.NS", "^NSEI", "^BSESN"):
            self.yahoo_has(sym, NEW)
        series = [Series(t, f"{t}.NS", t, self.store) for t in "ABC"]
        series += [self.idx, Series("^BSESN", "^BSESN", "BSESN", self.idx_store)]
        self.fetch(series)
        self.assertEqual([c for c, _ in self.yahoo.calls], [["A.NS", "B.NS"], ["C.NS"], ["^NSEI"], ["^BSESN"]])

    def test_too_many_rejects(self):
        self.assertTrue(ingest.too_many_rejects(self.cfg, 3, 10))
        self.assertFalse(ingest.too_many_rejects(self.cfg, 2, 10))  # exactly 20% is allowed
        self.assertFalse(ingest.too_many_rejects(self.cfg, 0, 0))

    def backfill(self, series, **kw):
        done = []
        got = backfill(self.fetcher(), self.cal, self.cfg, self.report, series, "2026-09-29", done.append)
        return got, done

    def test_backfill_rebuilds_and_signals_done(self):
        self.yahoo_has("A.NS", OLD + NEW)
        got, done = self.backfill([self.s])
        self.assertEqual((got, [s.ticker for s in done]), ({"A": 4}, ["A"]))
        self.assertEqual(len(self.store.read_fresh("A")), 2)
        self.assertEqual(len(self.store.read_archive("A")), 2)
        self.assertEqual(self.report.updated, {"A"})
        self.assertEqual(self.yahoo.calls[0][1], {"period": "max"})

    def test_backfill_empty_response_is_done_without_files(self):
        got, done = self.backfill([self.s])
        self.assertEqual((got, len(done)), ({"A": 0}, 1))
        self.assertFalse(self.store.fresh("A").exists())

    def test_backfill_rolls_back_over_20_percent_rejected(self):
        df = self.yahoo_has("A.NS", weekdays("2026-09-21", "2026-09-29"))  # 7 rows
        df.loc[:1, "High"] = 1.0
        got, done = self.backfill([self.s])
        self.assertEqual(done, [])
        self.assertIn("rolled back", self.report.failed["A"])
        self.assertFalse(self.store.fresh("A").exists())

    def test_backfill_keeps_existing_history_when_rolled_back(self):
        self.stored("A", NEW, close=55)
        df = self.yahoo_has("A.NS", NEW, close=100, High=1.0)
        self.backfill([self.s])
        self.assertEqual(set(self.store.read_fresh("A").Close), {55.0})

    def test_backfill_commits_valid_rows_and_quarantines_bad_under_threshold(self):
        df = self.yahoo_has("A.NS", weekdays("2026-09-14", "2026-09-29"))  # 12 rows
        df.loc[0, "High"] = 1.0
        self.backfill([self.s])
        self.assertEqual(len(self.store.read_fresh("A")), 11)
        self.assertEqual(len(self.report.rejected()), 1)

    def test_backfill_write_error_is_a_failure_not_a_crash(self):
        self.yahoo_has("A.NS", NEW)
        with patch.object(Store, "rebuild", side_effect=OSError("disk")):
            got, done = self.backfill([self.s])
        self.assertEqual(done, [])
        self.assertIn("write failed", self.report.failed["A"])
