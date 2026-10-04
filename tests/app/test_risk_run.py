"""Run end to end: gate conditions, targets wait, idempotence, --force, status file, files written, housekeeping, digest."""

import json
import unittest
from datetime import datetime, timedelta

import pandas as pd

from app.market.common import IST
from app.risk import common, run
from app.risk.common import Gate, Report
from tests.app.risk_helpers import FRIDAY, NOW, THURSDAY, Env

SUNDAY_LATE = datetime(2026, 9, 27, 22, 30, tzinfo=IST)
SATURDAY = datetime(2026, 9, 26, 10, 0, tzinfo=IST)


class RunEnv(Env):
    def setUp(self):
        super().setUp()
        for t in ("ABC", "DEF"):
            self.price(t)
        self.buckets(SmallCap=["ABC", "DEF", "XYZ"])
        self.targets({"SmallCap": ["ABC", "DEF"]})
        self.surveillance()

    def ok(self, **kw):
        report = self.run_risk(**kw)
        self.assertEqual(report.status(), "ok", report.error)
        return report


class GateTests(RunEnv):
    def gate(self, **kw):
        with self.assertRaises(Gate) as cm:
            self.run_risk(**kw)
        return str(cm.exception)

    def test_happy_path_writes_everything(self):
        report = self.ok()
        self.assertEqual(report.block["signals"], 2)
        for f in ("signals/signals_2026-09-25.json", "state/positions.csv", "state/cooldown.csv", "state/deferred_drops.csv", "state/ladder_state.json",
                  "nav/nav_actual.csv", "nav/nav_shadow.csv", "shadow/fills.csv", "shadow/cash.csv", "shadow/signals/signals_2026-09-25.json",
                  "shadow/state/ladder_state.json", "backup/2026-09-25/state/ladder_state.json"):
            self.assertTrue((self.risk / f).exists(), f)

    def test_market_status_must_be_ok_or_partial_for_asof_and_unlocked(self):
        self.market_status(status="failed")
        self.assertIn("has not finished", self.gate())
        self.market_status(last="2026-09-24")
        self.assertIn("has not finished", self.gate())
        self.market_status(lock=True)
        self.assertIn("in progress", self.gate())
        self.market_status(status="partial")
        self.ok()  # partial is accepted

    def test_status_without_last_trading_day_is_accepted(self):
        (self.market / "status.json").write_text(json.dumps({"status": "ok", "stage": "archiver"}))
        self.ok()

    def test_missing_status_file_is_not_ready(self):
        (self.market / "status.json").unlink()
        self.assertIn("has not finished", self.gate())

    def test_index_row_for_asof_is_required(self):
        self.index(days=self.days[:-1])
        self.idx_store.fresh("NSEI").unlink()
        self.index(days=self.days[:-1])
        self.assertIn("NSEI series has no row", self.gate())

    def test_ledger_must_have_succeeded_on_asof(self):
        self.ledger_status(status="failed")
        self.assertIn("Ledger has not succeeded", self.gate())
        self.ledger_status(good="2026-09-24")
        self.assertIn("Ledger has not succeeded", self.gate())
        self.cfg["gate"]["maxLedgerLagTradingDays"] = 1
        self.ok()

    def test_partial_ledger_run_warns_that_the_book_is_unreconciled(self):
        self.ledger_status(status="partial")
        self.run_risk()
        self.assertIn("LEDGER_PARTIAL", self.signals()["warnings"])

    def test_missing_ticker_share_above_10_percent_waits(self):
        for t in [f"T{i}" for i in range(8)]:
            self.price(t)
        self.targets({"SmallCap": ["ABC", "DEF"]}, top_n=10)
        self.book([(f"T{i}", 1, 250.0, "2026-01-05") for i in range(8)])
        self.price("T0", days=self.days[:-1])  # 1 of 10 needed (8 held + 2 selected) is exactly 10%: fine
        self.ok()
        self.price("T1", days=self.days[:-1])  # 2 of 10
        self.assertIn("2 of 10 tickers have no row", self.gate(force=True))

    def test_tickers_without_a_row_are_skipped_with_a_warning(self):
        extra = [f"T{i}" for i in range(9)]
        for t in extra:
            self.price(t)
        self.targets({"SmallCap": ["ABC", "DEF", "NOROW", *extra]}, top_n=20)  # 1 of 12 selected names has no row
        self.ok()
        self.assertIn("NO_ROW:NOROW", self.signals()["warnings"])

    def test_empty_holiday_list_for_the_year_fails_the_run(self):
        self.set_calendar([])
        with self.assertRaisesRegex(ValueError, "no holidays for 2026"):
            self.run_risk()

    def test_missing_opening_row_fails_fast_naming_the_file(self):
        self.flows([("2026-01-02", "DEPOSIT", 5)])
        with self.assertRaisesRegex(ValueError, "cash_flows.csv"):
            self.run_risk()
        self.assertFalse((self.risk / "signals").exists())

    def test_nothing_is_written_when_the_gate_waits(self):
        self.ledger_status(status="failed")
        self.gate()
        for sub in ("signals", "state", "nav", "shadow", "risk_status.json"):
            self.assertFalse((self.risk / sub).exists(), sub)


class TargetsTests(RunEnv):
    def test_rebalance_day_waits_for_targets_until_sunday_22_00(self):
        (self.analyst / "targets" / f"targets_{FRIDAY}.json").unlink()
        for now in (NOW_FRI_LATE, SATURDAY, datetime(2026, 9, 27, 21, 59, tzinfo=IST)):
            with self.assertRaises(Gate):
                self.run_risk(now)

    def test_first_attempt_after_the_deadline_continues_without_targets(self):
        (self.analyst / "targets" / f"targets_{FRIDAY}.json").unlink()
        self.book([("XYZ", 40, 280.0, "2026-01-05")])
        self.price("XYZ", closes=[300.0] * 399 + [245.0])
        report = self.run_risk(SUNDAY_LATE)
        self.assertEqual(report.status(), "ok")
        sig = self.signals()
        self.assertEqual(sig["weekly"], {"included": False, "targetsFile": None})
        self.assertEqual([a["reason"] for a in sig["actions"]], ["STOP"])  # stop-outs are never held back
        self.assertTrue(any("no targets for week of 2026-09-25; exits only" in line for line in report.lines))
        self.assertFalse(report.block["weekly"])

    def test_without_targets_the_sizer_does_not_run(self):
        (self.analyst / "targets" / f"targets_{FRIDAY}.json").unlink()
        self.run_risk(SUNDAY_LATE)
        self.assertEqual(self.signals()["actions"], [])  # no ENTRY for the names a list would have selected

    def test_bad_targets_files_count_as_missing(self):
        for kw in ({"status": "failed"}, {"schema": 2}):
            self.targets({"SmallCap": ["ABC"]}, **kw)
            with self.assertRaises(Gate):
                self.run_risk()
        (self.analyst / "targets" / f"targets_{FRIDAY}.json").write_text("{not json")
        with self.assertRaises(Gate):
            self.run_risk()

    def test_a_non_rebalance_day_needs_no_targets(self):
        now = self.set_day(THURSDAY)
        (self.analyst / "targets" / f"targets_{FRIDAY}.json").unlink()
        self.surveillance(day=THURSDAY)
        self.price("ABC", days=self.days_to(THURSDAY))
        self.price("DEF", days=self.days_to(THURSDAY))
        report = self.run_risk(now)
        self.assertEqual(report.status(), "ok")
        self.assertEqual(self.signals()["weekly"], {"included": False, "targetsFile": None})

    def test_a_week_with_a_friday_holiday_rebalances_on_thursday(self):
        self.set_calendar(["2026-09-25"])  # Friday holiday: Thursday is the last trading day of the week
        now = self.set_day(THURSDAY)
        self.targets({"SmallCap": ["ABC"]}, day=THURSDAY)
        self.surveillance(day=THURSDAY)
        for t in ("ABC", "DEF"):
            self.price(t, days=self.days_to(THURSDAY))
        self.run_risk(now)
        sig = self.signals()
        self.assertEqual((sig["weekly"]["included"], [a["ticker"] for a in sig["actions"]]), (True, ["ABC"]))
        self.assertEqual(sig["executionDate"], "2026-09-28")  # the holiday is skipped

    def test_targets_regime_wins_on_the_rebalance_day_and_history_otherwise(self):
        self.targets({"SmallCap": ["ABC"]}, regime=("TREND", "WEAK"))
        self.run_risk()
        self.assertEqual(self.signals()["regime"], {"raw": "TREND", "active": "WEAK"})
        now = self.set_day(THURSDAY)
        self.regimes([("2026-09-18", "BEAR", "BEAR"), (FRIDAY, "BULL", "BULL")])
        self.surveillance(day=THURSDAY)
        for t in ("ABC", "DEF"):
            self.price(t, days=self.days_to(THURSDAY))
        self.run_risk(now)
        self.assertEqual(self.signals(THURSDAY)["regime"], {"raw": "BEAR", "active": "BEAR"})  # newest row on or before asOf

    def test_missing_regime_history_is_unknown(self):
        (self.analyst / "regime/regime_history.csv").unlink()
        self.targets({}, regime=("BULL", "BULL"))
        now = self.set_day(THURSDAY)
        self.surveillance(day=THURSDAY)
        for t in ("ABC", "DEF"):
            self.price(t, days=self.days_to(THURSDAY))
        self.run_risk(now)
        self.assertEqual(self.signals(THURSDAY)["regime"], {"raw": "Unknown", "active": "Unknown"})


NOW_FRI_LATE = datetime(2026, 9, 25, 23, 45, tzinfo=IST)


class IdempotenceTests(RunEnv):
    def test_second_run_for_the_same_asof_is_a_no_op(self):
        self.ok()
        before = (self.risk / "signals/signals_2026-09-25.json").read_text()
        report = self.run_risk()
        self.assertTrue(report.quiet)
        self.assertEqual((self.risk / "signals/signals_2026-09-25.json").read_text(), before)

    def test_saturday_retry_after_a_good_friday_run_is_no_new_bar(self):
        self.ok()
        self.assertTrue(self.run_risk(SATURDAY).quiet)

    def test_saturday_retry_still_produces_fridays_signals_when_missing(self):
        self.assertEqual(self.run_risk(SATURDAY).status(), "ok")
        self.assertEqual(self.signals()["asOf"], FRIDAY)

    def test_force_renames_the_old_file_and_writes_a_new_one(self):
        self.ok()
        self.run_risk(force=True)
        names = sorted(p.name for p in (self.risk / "signals").iterdir())
        self.assertEqual(len(names), 2)
        self.assertTrue(any(n.startswith("signals_2026-09-25.superseded_") for n in names))
        self.assertEqual(self.signals()["status"], "ok")

    def test_a_forced_rerun_does_not_duplicate_nav_rows_or_shadow_fills(self):
        self.ok()
        self.run_risk(force=True)
        self.assertEqual(len(pd.read_csv(self.risk / "nav/nav_actual.csv")), 1)
        self.assertEqual(len(pd.read_csv(self.risk / "nav/nav_shadow.csv")), 1)

    def test_a_failed_signal_file_does_not_block_a_rerun(self):
        folder = self.risk / "signals"
        folder.mkdir(parents=True)
        (folder / "signals_2026-09-25.json").write_text(json.dumps({"status": "failed"}))
        self.assertFalse(self.run_risk().quiet)


class FinalAttemptTests(RunEnv):
    def test_final_attempt_turns_an_unmet_gate_into_a_failed_run(self):
        self.ledger_status(status="failed")
        now = datetime(2026, 9, 28, 7, 45, tzinfo=IST)  # Friday's last attempt: Monday 07:45
        report = self.run_risk(now)
        self.assertEqual(report.status(), "failed")
        self.assertIn("no signals for 2026-09-25", report.error)

    def test_earlier_attempts_just_wait(self):
        self.ledger_status(status="failed")
        with self.assertRaises(Gate):
            self.run_risk(datetime(2026, 9, 28, 6, 45, tzinfo=IST))

    def test_final_attempt_times(self):
        cfg = self.cfg
        fri, thu = datetime(2026, 9, 25).date(), datetime(2026, 9, 24).date()
        self.assertTrue(run.final_attempt(cfg, datetime(2026, 9, 28, 7, 0, tzinfo=IST), fri))
        self.assertFalse(run.final_attempt(cfg, datetime(2026, 9, 28, 6, 59, tzinfo=IST), fri))
        self.assertTrue(run.final_attempt(cfg, datetime(2026, 9, 25, 7, 30, tzinfo=IST), thu))
        self.assertFalse(run.final_attempt(cfg, datetime(2026, 9, 24, 23, 45, tzinfo=IST), thu))

    def test_targets_deadline_is_sunday_of_the_asof_week(self):
        self.assertEqual(run.targets_deadline(self.cfg, datetime(2026, 9, 25).date()), datetime(2026, 9, 27, 22, 0, tzinfo=IST))

    def test_failed_run_writes_status_but_keeps_the_last_good_asof(self):
        report = Report("run", keep=("lastGoodAsOf",))
        report.error = "boom"
        self.risk.mkdir(parents=True, exist_ok=True)
        (self.risk / "risk_status.json").write_text(json.dumps({"run": {"status": "ok", "lastGoodAsOf": "2026-09-24"}}))
        common.write_status(self.cfg, report, NOW_FRI_LATE)
        block = json.loads((self.risk / "risk_status.json").read_text())["run"]
        self.assertEqual((block["status"], block["lastGoodAsOf"]), ("failed", "2026-09-24"))


class StatusAndOutputTests(RunEnv):
    def test_status_block_fields(self):
        report = self.ok()
        common.write_status(self.cfg, report, NOW_FRI_LATE)
        block = json.loads((self.risk / "risk_status.json").read_text())["run"]
        self.assertEqual({k: block[k] for k in ("status", "asOf", "lastGoodAsOf", "signals", "weekly", "rung", "signalsFile")},
                         {"status": "ok", "asOf": FRIDAY, "lastGoodAsOf": FRIDAY, "signals": 2, "weekly": True, "rung": 0, "signalsFile": "signals/signals_2026-09-25.json"})

    def test_signal_file_follows_the_schema(self):
        self.ok()
        sig = self.signals()
        self.assertEqual([k for k in sig], ["schemaVersion", "runId", "generatedAt", "status", "asOf", "executionDate", "executionAt", "weekly", "regime", "nav", "ladder",
                                            "exposure", "surveillance", "actions", "holds", "blocked", "positions", "untracked", "warnings"])
        self.assertEqual((sig["schemaVersion"], sig["executionAt"], sig["runId"]), (1, "open", "risk-2026-09-25T21:45:00+05:30"))
        self.assertEqual(set(sig["actions"][0]), {"ticker", "bucket", "side", "kind", "qty", "refPriceInr", "notionalInr", "reason", "estChargesInr", "priority", "detail"})
        self.assertEqual(set(sig["nav"]), {"navInr", "cashInr", "positionsValueInr", "investedPct", "twrIndex", "peakIndex", "drawdownPct"})
        self.assertEqual(set(sig["exposure"]), {"regimeCap", "ladderCap", "finalCap", "heatPct", "heatCapPct"})
        self.assertEqual(set(sig["ladder"]), {"rung", "maxInvestedPct", "reRiskEligible", "flatLocked"})
        self.assertNotIn("orderType", json.dumps(sig))  # signals only: no order types, limit prices or broker fields

    def test_nav_row_is_complete(self):
        self.ok()
        row = pd.read_csv(self.risk / "nav/nav_actual.csv").iloc[0]
        self.assertEqual(list(row.index), ["date", "positions_value", "cash", "nav", "flow", "twr_index", "bench_close", "active_regime", "rung"])
        self.assertEqual((row.nav, row.twr_index, row.bench_close, row.active_regime, row.rung), (700000.0, 1.0, 20000.0, "BULL", 0))

    def test_untracked_holdings_come_from_the_ledger_status(self):
        self.ledger_status(untracked=["ABCD"])
        self.ok()
        self.assertEqual(self.signals()["untracked"], ["ABCD"])

    def test_nav_jump_warns_and_never_blocks(self):
        self.ladder = None
        nav_dir = self.risk / "nav"
        nav_dir.mkdir(parents=True)
        pd.DataFrame([{"date": "2026-09-24", "positions_value": 0, "cash": 500000, "nav": 500000, "flow": 0, "twr_index": 1.0, "bench_close": 1, "active_regime": "BULL", "rung": 0}]
                     ).to_csv(nav_dir / "nav_actual.csv", index=False)
        self.ok()
        self.assertIn("NAV_JUMP", self.signals()["warnings"])

    def test_digest_summarises_the_run(self):
        report = self.ok()
        text = "\n".join(report.digest(NOW_FRI_LATE))
        for s in ("Gate: passed", "NAV Rs 700,000", "ladder rung 0", "Exposure caps", "Signals: ENTRY 2"):
            self.assertIn(s, text)
        self.assertIn("[Risk] run OK", report.digest(NOW_FRI_LATE)[0])

    def test_digest_lists_blocked_entries_and_missing_surveillance(self):
        (self.risk / "surveillance" / f"surveillance_{FRIDAY}.json").unlink()
        report = self.ok()
        text = "\n".join(report.lines)
        self.assertIn("Blocked: ABC ENTRY NO_SURVEILLANCE_DATA", text)
        self.assertIn("Surveillance list missing", text)

    def test_cooldown_row_is_removed_after_the_reentry_signal(self):
        (self.risk / "state").mkdir(parents=True)
        pd.DataFrame([{"ticker": "ABC", "trigger_date": "2026-09-01", "trigger_adj_close": 200.0, "release_after": "2026-09-15"}]).to_csv(self.risk / "state/cooldown.csv", index=False)
        self.ok()
        self.assertEqual(len(pd.read_csv(self.risk / "state/cooldown.csv")), 0)
        self.assertIn("ABC", self.actions())

    def test_cooldown_blocks_a_reselected_name_until_released(self):
        (self.risk / "state").mkdir(parents=True)
        pd.DataFrame([{"ticker": "ABC", "trigger_date": "2026-09-20", "trigger_adj_close": 200.0, "release_after": "2026-10-04"}]).to_csv(self.risk / "state/cooldown.csv", index=False)
        self.ok()
        self.assertEqual(self.signals()["blocked"], [{"ticker": "ABC", "intent": "ENTRY", "reason": "COOLDOWN"}])
        self.assertEqual(len(pd.read_csv(self.risk / "state/cooldown.csv")), 1)

    def test_cooldown_rows_expire_after_52_weeks(self):
        (self.risk / "state").mkdir(parents=True)
        pd.DataFrame([{"ticker": "OLD", "trigger_date": "2025-08-01", "trigger_adj_close": 200.0, "release_after": "2025-08-15"}]).to_csv(self.risk / "state/cooldown.csv", index=False)
        self.ok()
        self.assertEqual(len(pd.read_csv(self.risk / "state/cooldown.csv")), 0)

    def test_sell_signals_come_before_buy_signals(self):
        self.price("XYZ", closes=[300.0] * 399 + [245.0])
        self.book([("XYZ", 40, 280.0, "2026-01-05")])
        self.ok()
        acts = self.signals()["actions"]
        self.assertEqual([a["side"] for a in acts], ["SELL", "BUY", "BUY"])
        self.assertEqual(acts[0]["ticker"], "XYZ")

    def test_the_book_may_be_missing_before_the_ledger_has_ever_written_one(self):
        (self.analyst / "ledger/book.csv").unlink()
        self.ok()
        self.assertEqual(self.signals()["positions"], [])


class HousekeepingTests(RunEnv):
    def touch(self, rel, days_old):
        path = self.risk / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")
        return path

    def test_old_files_are_purged_and_recent_ones_kept(self):
        today = NOW.date()
        d = lambda n: str(today - timedelta(days=n))  # noqa: E731
        old = [self.touch(f"backup/{d(31)}/state/x.json", 0), self.touch(f"surveillance/raw/asm_{d(31)}.csv", 0), self.touch(f"surveillance/surveillance_{d(91)}.json", 0),
               self.touch("signals/signals_2024-01-05.json", 0), self.touch("shadow/signals/signals_2024-01-05.json", 0)]
        new = [self.touch(f"backup/{d(29)}/state/x.json", 0), self.touch(f"surveillance/raw/asm_{d(29)}.csv", 0), self.touch(f"surveillance/surveillance_{d(89)}.json", 0),
               self.touch("signals/signals_2026-09-18.json", 0)]
        self.ok()
        self.assertFalse(any(p.exists() for p in old), [p for p in old if p.exists()])
        self.assertTrue(all(p.exists() for p in new), [p for p in new if not p.exists()])
        self.assertTrue((self.risk / "signals/signals_2026-09-25.json").exists())

    def test_a_housekeeping_failure_is_reported_but_does_not_fail_the_run(self):
        from unittest.mock import patch
        with patch("app.risk.run.shutil.copytree", side_effect=OSError("disk full")):
            report = self.run_risk()
        self.assertEqual(report.status(), "ok")
        self.assertTrue(any("Backup/purge failed" in line for line in report.lines))
        self.assertTrue((self.risk / "signals/signals_2026-09-25.json").exists())

    def test_backup_copies_state_nav_and_shadow(self):
        self.ok()
        backup = self.risk / "backup" / "2026-09-25"
        for sub in ("state/ladder_state.json", "nav/nav_actual.csv", "shadow/fills.csv"):
            self.assertTrue((backup / sub).exists(), sub)


if __name__ == "__main__":
    unittest.main()
