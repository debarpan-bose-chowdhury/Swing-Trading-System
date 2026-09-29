"""End-to-end stage behaviour: Migrator, Updator, Archiver against a scripted Yahoo."""

import json
from datetime import datetime
from unittest.mock import patch

import pandas as pd

from app.market import archiver, migrator, updator
from app.market.common import IST, Report
from app.market.fetcher import Blocked
from app.market.store import Store
from tests.market_helpers import CUTOFF, LOG, NOW, TODAY, Env, bars, weekdays

HISTORY = weekdays("2026-08-31", "2026-09-28")  # stored through Monday
LONG = weekdays("2025-01-01", "2026-09-29")


class StageEnv(Env):
    def run_stage(self, module, now=NOW, report=None):
        self.report = report or Report(module.__name__.rsplit(".", 1)[1])
        module.run(self.cfg, now, LOG, self.report, *([self.fetcher()] if module is not archiver else []))
        return self.report

    def seed(self, tickers=("AAA",), days=HISTORY):
        self.set_registry(list(tickers))
        for t in tickers:
            self.stored(t, days)
            self.yahoo_has(f"{t}.NS", days + ["2026-09-29"])


class UpdatorTests(StageEnv):
    def test_appends_new_day_with_one_day_overlap(self):
        self.seed()
        self.run_stage(updator)
        self.assertEqual(self.store.last_date("AAA"), "2026-09-29")
        self.assertEqual(self.yahoo.calls[0][1], {"start": "2026-09-28", "end": "2026-09-30"})
        self.assertEqual(self.report.updated, {"AAA"})
        self.assertEqual(self.report.status(), "ok")
        self.assertEqual(self.report.last_trading_day, TODAY)
        self.assertEqual(self.report.rebuilt, {})

    def test_nothing_to_do_makes_no_requests(self):
        self.seed()
        self.stored("AAA", HISTORY + ["2026-09-29"])
        self.run_stage(updator)
        self.assertEqual(self.yahoo.calls, [])

    def test_session_not_final_before_cutoff_time(self):
        self.seed()
        self.yahoo_has("AAA.NS", HISTORY + ["2026-09-29"])  # partial bar for today exists at Yahoo
        self.run_stage(updator, now=datetime(2026, 9, 29, 15, 0, tzinfo=IST))
        self.assertEqual(self.store.last_date("AAA"), "2026-09-28")  # no partial bar stored

    def test_lookback_fills_missing_day_in_the_middle(self):
        self.seed(days=[d for d in HISTORY if d != "2026-09-16"])
        self.yahoo_has("AAA.NS", HISTORY + ["2026-09-29"])
        self.run_stage(updator)
        self.assertIn("2026-09-16", set(self.store.read_fresh("AAA").Date))
        self.assertEqual(self.yahoo.calls[0][1]["start"], "2026-09-16")

    def test_lookback_ignores_days_before_first_stored_row(self):
        self.seed(days=weekdays("2026-09-22", "2026-09-28"))
        self.run_stage(updator)
        self.assertEqual(self.yahoo.calls[0][1]["start"], "2026-09-28")

    def test_gap_beyond_lookback_window_is_not_scanned(self):
        self.seed(days=["2026-08-03"] + weekdays("2026-08-31", "2026-09-28"))  # hole 08-04..08-28 is > 30 days old
        self.run_stage(updator)
        self.assertEqual(self.yahoo.calls[0][1]["start"], "2026-09-28")

    def test_new_ticker_is_backfilled_and_split_at_cutoff(self):
        self.set_registry(["NEW"])
        self.yahoo_has("NEW.NS", LONG)
        self.run_stage(updator)
        self.assertEqual(self.store.read_fresh("NEW").Date.min(), CUTOFF)
        self.assertLess(self.store.read_archive("NEW").Date.max(), CUTOFF)
        self.assertEqual(self.yahoo.calls[0][1], {"period": "max"})

    def test_dividend_on_new_day_rebuilds_full_history(self):
        self.seed()
        self.store.rebuild("AAA", bars("AAA", weekdays("2025-01-01", "2026-09-28"), close=100))
        self.yahoo_has("AAA.NS", LONG, close=90, Dividends=0.0)
        self.yahoo.data["AAA.NS"].loc[self.yahoo.data["AAA.NS"].Date == TODAY, "Dividends"] = 5.0
        self.run_stage(updator)
        self.assertIn("dividend 2026-09-29", self.report.rebuilt["AAA"])
        self.assertEqual(set(self.store.read_fresh("AAA").AdjClose), {90.0})
        self.assertEqual(set(self.store.read_archive("AAA").AdjClose), {90.0})
        self.assertEqual(self.yahoo.calls[-1][1], {"period": "max"})

    def test_split_rebuilds_and_restated_history_replaces_archive(self):
        self.seed()
        self.store.rebuild("AAA", bars("AAA", weekdays("2025-01-01", "2026-09-28"), close=100))
        data = self.yahoo_has("AAA.NS", LONG, close=50)
        data.loc[data.Date == TODAY, "Splits"] = 2.0
        self.run_stage(updator)
        self.assertIn("split 2026-09-29", self.report.rebuilt["AAA"])
        self.assertEqual(set(self.store.read_archive("AAA").Close), {50.0})
        self.assertEqual(self.store.last_date("AAA"), TODAY)

    def test_overlap_disagreement_triggers_rebuild(self):
        self.seed()
        self.yahoo_has("AAA.NS", LONG, close=50)  # Yahoo now reports 50 where 100 is stored
        self.run_stage(updator)
        self.assertEqual(self.report.rebuilt, {"AAA": "stored prices restated by Yahoo"})
        self.assertEqual(set(self.store.read_fresh("AAA").Close), {50.0})

    def test_dividend_on_already_stored_overlap_day_is_not_a_new_event(self):
        self.seed()
        data = self.yahoo_has("AAA.NS", HISTORY + [TODAY])
        data.loc[data.Date == "2026-09-28", "Dividends"] = 5.0
        self.run_stage(updator)
        self.assertEqual(self.report.rebuilt, {})

    def test_failed_rebuild_keeps_old_history(self):
        self.seed()
        self.yahoo_has("AAA.NS", LONG, close=50)
        self.yahoo.script = [None] + [OSError()] * 4  # incremental fetch works, max fetch fails
        self.run_stage(updator)
        self.assertEqual(set(self.store.read_fresh("AAA").Close), {100.0})
        self.assertIn("AAA", self.report.failed)

    def test_bad_rows_are_quarantined_and_good_rows_commit(self):
        self.seed(days=weekdays("2026-08-31", "2026-09-22"))  # 6 rows come back, 1 bad (< 20%)
        data = self.yahoo_has("AAA.NS", HISTORY + [TODAY, "2026-10-03"])
        data.loc[data.Date == TODAY, "High"] = 1.0
        self.run_stage(updator)
        self.assertEqual(self.store.last_date("AAA"), "2026-09-28")
        self.assertEqual(list(self.report.rejected().Reason), ["OHLC"])
        self.assertEqual(self.report.status(), "ok")

    def test_quarantined_day_is_retried_next_run(self):
        self.seed(days=weekdays("2026-08-31", "2026-09-22"))
        data = self.yahoo_has("AAA.NS", HISTORY + [TODAY])
        data.loc[data.Date == TODAY, "High"] = 1.0
        self.run_stage(updator)
        self.yahoo_has("AAA.NS", HISTORY + [TODAY])
        self.run_stage(updator)
        self.assertEqual(self.store.last_date("AAA"), TODAY)

    def test_weekend_row_from_yahoo_is_rejected(self):
        self.seed(days=weekdays("2026-08-31", "2026-09-22"))
        self.yahoo_has("AAA.NS", weekdays("2026-08-31", "2026-09-29") + ["2026-09-26"])
        self.run_stage(updator)
        self.assertEqual(list(self.report.rejected().Reason), ["NON_TRADING_DAY"])

    def test_more_than_20_percent_rejected_rolls_back_ticker(self):
        self.seed(days=weekdays("2026-08-31", "2026-09-22"))
        data = self.yahoo_has("AAA.NS", weekdays("2026-09-22", TODAY))  # 6 rows returned
        data.loc[1:2, "High"] = 1.0
        self.run_stage(updator)
        self.assertEqual(self.store.last_date("AAA"), "2026-09-22")
        self.assertIn("rolled back", self.report.failed["AAA"])
        self.assertEqual(self.report.status(), "partial")

    def test_fetch_failure_only_affects_that_ticker(self):
        self.seed(("AAA", "BBB"))
        self.yahoo.script = [OSError()] * 4  # first batch call dies... both are in the same batch
        self.cfg["fetch"]["batchSize"] = 1
        self.run_stage(updator)
        self.assertEqual(set(self.report.failed), {"AAA"})
        self.assertEqual(self.store.last_date("BBB"), TODAY)
        self.assertEqual(self.store.last_date("AAA"), "2026-09-28")

    def test_rate_limit_pause_then_resume_completes_everything(self):
        self.seed(("AAA", "BBB"))
        self.yahoo.script = [Blocked()]
        self.run_stage(updator)
        self.assertIn(3600, self.sleeps)
        self.assertEqual({self.store.last_date(t) for t in ("AAA", "BBB")}, {TODAY})

    def test_rate_limit_exhausted_defers_remaining_to_next_run(self):
        self.seed(("AAA", "BBB"))
        self.cfg["fetch"]["batchSize"] = 1
        self.yahoo.script = [None, *[Blocked()] * 4]
        self.run_stage(updator)
        self.assertEqual(self.store.last_date("AAA"), TODAY)
        self.assertIn("deferred", self.report.failed["BBB"])
        self.assertEqual(self.store.last_date("BBB"), "2026-09-28")
        self.yahoo.script = []  # next day's run picks it up
        self.run_stage(updator)
        self.assertEqual(self.store.last_date("BBB"), TODAY)
        self.assertEqual(self.report.failed, {})

    def test_registry_refreshed_when_upstream_healthy(self):
        self.seed()
        self.write_upstream(["AAA", "NEWCO"])
        self.yahoo_has("NEWCO.NS", LONG)
        self.run_stage(updator)
        self.assertTrue(self.report.registry_refreshed)
        self.assertIn("NEWCO", self.registry().index)
        self.assertIsNotNone(self.store.last_date("NEWCO"))  # brand-new ticker backfilled in the same run

    def test_registry_not_refreshed_when_upstream_unhealthy_but_prices_update(self):
        self.seed()
        self.write_upstream(["AAA", "NEWCO"], healthy=False)
        self.run_stage(updator)
        self.assertFalse(self.report.registry_refreshed)
        self.assertNotIn("NEWCO", self.registry().index)
        self.assertTrue(any("registry not refreshed" in n for n in self.report.notes))
        self.assertEqual(self.store.last_date("AAA"), TODAY)

    def test_stale_health_json_skips_refresh(self):
        self.seed()
        self.write_upstream(["NEWCO"], checked="2026-09-28T19:45:00+05:30")
        self.run_stage(updator)
        self.assertFalse(self.report.registry_refreshed)

    def test_index_series_are_updated_in_their_own_folder(self):
        self.set_indices(["^NSEI"])
        self.set_registry([])
        self.stored("^NSEI", HISTORY, store=self.idx_store)
        self.yahoo_has("^NSEI", HISTORY + [TODAY], Volume=0)
        self.run_stage(updator)
        self.assertEqual(self.idx_store.last_date("NSEI"), TODAY)
        self.assertTrue((self.market / "indices" / "fresh" / "NSEI.csv").exists())
        self.assertEqual(list(self.idx_store.read_fresh("NSEI").Ticker.unique()), ["^NSEI"])

    def run_days(self, days):
        for day in days:
            self.yahoo_has("AAA.NS", weekdays("2026-08-31", day))
            self.run_stage(updator, now=datetime.fromisoformat(f"{day}T21:00:00+05:30"))

    def test_dead_ticker_goes_inactive_after_five_trading_days(self):
        self.seed(("AAA", "DEAD"))
        del self.yahoo.data["DEAD.NS"]
        days = ["2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02", "2026-10-05"]
        for i, day in enumerate(days, 1):
            self.run_days([day])
            reg = self.registry()
            self.assertEqual(reg.at["DEAD", "no_data_days"], i if i < 5 else 5)
            self.assertEqual(reg.at["DEAD", "status"], "active" if i < 5 else "inactive")
        self.assertEqual(self.report.inactive, ["DEAD"])
        self.assertEqual(self.registry().at["DEAD", "inactive_since"], "2026-10-05")
        self.assertEqual(self.report.summary(NOW)["tickersInactive"], ["DEAD"])
        self.assertTrue(self.store.fresh("DEAD").exists())  # history kept
        self.yahoo.calls.clear()
        self.run_days(["2026-10-06"])
        self.assertNotIn("DEAD.NS", self.yahoo.symbols())

    def test_data_resets_the_no_data_counter(self):
        self.seed(("AAA", "FLAKY"))
        self.yahoo.data.pop("FLAKY.NS")
        self.run_days(["2026-09-29", "2026-09-30"])
        self.assertEqual(self.registry().at["FLAKY", "no_data_days"], 2)
        self.yahoo.data["FLAKY.NS"] = bars("FLAKY.NS", weekdays("2026-08-31", "2026-10-01"))
        self.run_days(["2026-10-01"])
        self.assertEqual(self.registry().at["FLAKY", "no_data_days"], 0)

    def test_systemic_empty_responses_do_not_count_against_tickers(self):
        self.seed(("AAA", "BBB"))
        self.yahoo.data.clear()
        self.run_stage(updator)
        self.assertEqual(list(self.registry().no_data_days), [0, 0])

    def test_failed_fetch_is_not_a_no_data_day(self):
        self.seed(("AAA", "BBB"))
        self.cfg["fetch"]["batchSize"] = 1
        self.yahoo.script = [None, *[OSError()] * 4]
        self.run_stage(updator)
        self.assertEqual(self.registry().at["BBB", "no_data_days"], 0)

    def test_holiday_only_gap_does_not_count_as_no_data(self):
        # the calendar knows nothing about 2026-09-16, so it looks like a missing trading day forever
        self.seed(("AAA", "BBB"), days=[d for d in HISTORY if d != "2026-09-16"] + ["2026-09-29"])
        self.yahoo_has("AAA.NS", HISTORY)
        self.yahoo.data.pop("BBB.NS")
        self.run_stage(updator)
        self.assertEqual(list(self.registry().no_data_days), [0, 0])

    def test_holiday_on_last_day_uses_previous_session(self):
        self.set_calendar(holidays=["2026-09-29"])
        self.seed()
        self.run_stage(updator)
        self.assertEqual(self.report.last_trading_day, "2026-09-28")
        self.assertEqual(self.yahoo.calls, [])

    def test_inactive_tickers_are_not_fetched(self):
        self.seed(("AAA", "OLD"))
        reg = self.registry()
        reg.loc["OLD", "status"] = "inactive"
        from app.market import registry
        registry.save(reg, self.market / "registry.csv")
        self.run_stage(updator)
        self.assertNotIn("OLD.NS", self.yahoo.symbols())

    def test_reactivated_ticker_is_fetched_again(self):
        self.seed(("AAA",))
        self.set_registry(["OLD"])
        reg = self.registry()
        reg.loc["OLD", ["status", "inactive_since", "absent_since_inactive"]] = ["inactive", "2026-09-10", True]
        from app.market import registry
        registry.save(reg, self.market / "registry.csv")
        self.write_upstream(["AAA", "OLD"])
        self.yahoo_has("OLD.NS", LONG)
        self.run_stage(updator)
        self.assertEqual(self.registry().at["OLD", "status"], "active")
        self.assertIn("re-activated OLD", self.report.notes)
        self.assertIsNotNone(self.store.last_date("OLD"))

    def test_unique_key_holds_after_repeated_runs(self):
        self.seed()
        for _ in range(3):
            self.run_stage(updator)
        df = self.store.read_fresh("AAA")
        self.assertFalse(df.duplicated(["Ticker", "Date"]).any())
        self.assertEqual(list(df.Date), sorted(df.Date))


class MigratorTests(StageEnv):
    def setup_upstream(self, tickers=("AAA", "BBB")):
        self.write_upstream(list(tickers))
        for t in tickers:
            self.yahoo_has(f"{t}.NS", LONG)
        self.set_indices(["^NSEI"])
        self.yahoo_has("^NSEI", LONG, Volume=0)

    def test_full_bootstrap(self):
        self.setup_upstream()
        self.run_stage(migrator)
        for t in ("AAA", "BBB"):
            self.assertEqual(self.store.read_fresh(t).Date.min(), CUTOFF)
            self.assertLess(self.store.read_archive(t).Date.max(), CUTOFF)
        self.assertEqual(self.idx_store.last_date("NSEI"), TODAY)
        self.assertEqual(list(self.registry().index), ["AAA", "BBB"])
        self.assertTrue((self.market / "migration.done").exists())
        self.assertEqual(self.report.status(), "ok")
        self.assertEqual(len(json.loads((self.market / "migration_checkpoint.json").read_text())), 3)

    def test_second_run_is_a_noop(self):
        self.setup_upstream()
        self.run_stage(migrator)
        self.yahoo.calls.clear()
        self.run_stage(migrator)
        self.assertEqual(self.yahoo.calls, [])
        self.assertTrue(self.report.quiet)

    def test_refuses_when_upstream_unhealthy(self):
        self.setup_upstream()
        self.write_upstream(["AAA"], healthy=False)
        with self.assertRaisesRegex(RuntimeError, "not healthy"):
            self.run_stage(migrator)
        self.assertFalse((self.market / "registry.csv").exists())
        self.assertEqual(self.yahoo.calls, [])

    def test_refuses_when_health_is_stale(self):
        self.setup_upstream()
        self.write_upstream(["AAA"], checked="2026-09-27T19:45:00+05:30")
        with self.assertRaises(RuntimeError):
            self.run_stage(migrator)

    def test_one_failure_leaves_no_marker_and_rerun_resumes_only_the_rest(self):
        self.setup_upstream()
        self.cfg["fetch"]["batchSize"] = 1
        self.yahoo.script = [None, *[OSError()] * 4]  # AAA ok, BBB fails, index ok
        self.run_stage(migrator)
        self.assertFalse((self.market / "migration.done").exists())
        self.assertEqual(set(self.report.failed), {"BBB"})
        self.assertEqual(sorted(json.loads((self.market / "migration_checkpoint.json").read_text())), ["AAA", "^NSEI"])
        self.yahoo.calls.clear()
        self.run_stage(migrator)
        self.assertEqual(self.yahoo.symbols(), ["BBB.NS"])
        self.assertTrue((self.market / "migration.done").exists())

    def test_crash_mid_run_resumes_from_checkpoint(self):
        self.setup_upstream()
        self.cfg["fetch"]["batchSize"] = 1
        real = Store.rebuild
        calls = []

        def crashing(store, key, rows):
            calls.append(key)
            if len(calls) == 2:
                raise KeyboardInterrupt  # simulates the process dying mid-ticker
            return real(store, key, rows)

        with patch.object(Store, "rebuild", crashing), self.assertRaises(KeyboardInterrupt):
            self.run_stage(migrator)
        self.assertEqual(json.loads((self.market / "migration_checkpoint.json").read_text()), ["AAA"])
        self.assertFalse(self.store.fresh("BBB").exists())
        self.yahoo.calls.clear()
        self.run_stage(migrator)
        self.assertNotIn("AAA.NS", self.yahoo.symbols())
        self.assertTrue((self.market / "migration.done").exists())

    def test_bad_response_does_not_corrupt_and_is_not_checkpointed(self):
        self.setup_upstream(("AAA",))
        self.yahoo.data["AAA.NS"] = bars("AAA.NS", LONG, High=1.0)
        self.run_stage(migrator)
        self.assertIn("rolled back", self.report.failed["AAA"])
        self.assertFalse(self.store.fresh("AAA").exists())
        self.assertFalse((self.market / "migration.done").exists())

    def test_ticker_with_no_yahoo_data_does_not_block_completion(self):
        self.setup_upstream(("AAA", "GHOST"))
        del self.yahoo.data["GHOST.NS"]
        self.run_stage(migrator)
        self.assertTrue((self.market / "migration.done").exists())
        self.assertFalse(self.store.fresh("GHOST").exists())

    def test_todays_partial_bar_is_not_stored_before_session_final(self):
        self.setup_upstream(("AAA",))
        self.run_stage(migrator, now=datetime(2026, 9, 29, 19, 45, tzinfo=IST))
        self.assertEqual(self.store.last_date("AAA"), "2026-09-28")

    def test_updator_continues_where_migrator_left_off(self):
        self.setup_upstream(("AAA",))
        self.write_upstream(["AAA"], checked="2026-09-28T19:45:00+05:30")
        self.run_stage(migrator, now=datetime(2026, 9, 28, 21, 0, tzinfo=IST))
        self.assertEqual(self.store.last_date("AAA"), "2026-09-28")
        self.run_stage(updator)
        self.assertEqual(self.store.last_date("AAA"), TODAY)
        self.assertEqual(self.report.rebuilt, {})


class ArchiverTests(StageEnv):
    def test_moves_aged_rows_for_tickers_and_indices(self):
        self.set_indices(["^NSEI"])
        self.set_registry(["AAA", "BBB"])
        for t in ("AAA", "BBB"):
            self.store.upsert(t, bars(t, ["2025-01-02", "2025-09-26", CUTOFF, TODAY]))
        self.idx_store.upsert("NSEI", bars("^NSEI", ["2025-01-02", TODAY]))
        self.run_stage(archiver)
        self.assertEqual(self.report.updated, {"AAA", "BBB", "^NSEI"})
        self.assertEqual(list(self.store.read_fresh("AAA").Date), [CUTOFF, TODAY])
        self.assertEqual(list(self.store.read_archive("AAA").Date), ["2025-01-02", "2025-09-26"])
        self.assertEqual(list(self.idx_store.read_archive("NSEI").Date), ["2025-01-02"])
        self.assertTrue((self.market / "indices" / "archive" / "NSEI").is_dir())

    def test_uses_cutoff_of_365_days_before_run_date(self):
        self.set_registry(["AAA"])
        self.store.upsert("AAA", bars("AAA", ["2025-09-26", "2025-09-29", "2025-09-30"]))
        self.run_stage(archiver)
        self.assertEqual(list(self.store.read_archive("AAA").Date), ["2025-09-26"])

    def test_inactive_tickers_are_archived_too(self):
        self.set_registry(["OLD"], status="inactive")
        self.store.upsert("OLD", bars("OLD", ["2025-01-02", TODAY]))
        self.run_stage(archiver)
        self.assertEqual(len(self.store.read_archive("OLD")), 1)

    def test_nothing_aged_is_a_clean_noop(self):
        self.set_registry(["AAA"])
        self.stored("AAA", HISTORY)
        self.run_stage(archiver)
        self.assertEqual((self.report.updated, self.report.failed), (set(), {}))

    def test_one_failing_ticker_does_not_stop_the_rest(self):
        self.set_registry(["AAA", "BBB"])
        for t in ("AAA", "BBB"):
            self.store.upsert(t, bars(t, ["2025-01-02", TODAY]))
        real = Store.archive_aged

        def flaky(store, key):
            if key == "AAA":
                raise OSError("disk")
            return real(store, key)

        with patch.object(Store, "archive_aged", flaky):
            self.run_stage(archiver)
        self.assertIn("AAA", self.report.failed)
        self.assertEqual(self.report.updated, {"BBB"})
        self.assertEqual(self.report.status(), "partial")

    def test_second_run_is_idempotent(self):
        self.set_registry(["AAA"])
        self.store.upsert("AAA", bars("AAA", ["2025-01-02", TODAY]))
        self.run_stage(archiver)
        self.run_stage(archiver)
        self.assertEqual(len(list((self.market / "archive" / "AAA").glob("*.parquet"))), 1)
        self.assertEqual(self.report.updated, set())

    def test_missing_registry_archives_only_indices(self):
        self.set_indices(["^NSEI"])
        self.idx_store.upsert("NSEI", bars("^NSEI", ["2025-01-02", TODAY]))
        self.run_stage(archiver)
        self.assertEqual(self.report.updated, {"^NSEI"})
