"""Validator, calendar, common (lock, report, run_stage), mailer, registry."""

import json
import os
import smtplib
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

from app.market import common, mailer, registry
from app.market.common import IST, Busy, Report
from app.market.tradingcal import Calendar
from app.market.validator import validate
from tests.market_helpers import LOG, NOW, TODAY, Env, bars, make_cfg


class ValidatorTests(Env):
    def setUp(self) -> None:
        super().setUp()
        self.cal = Calendar(self.cfg["paths"]["calendar"])

    def check(self, df: pd.DataFrame, expected: str | None) -> pd.DataFrame:
        valid, rej = validate(df, self.cal)
        if expected is None:
            self.assertTrue(rej.empty)
            self.assertEqual(len(valid), len(df))
        else:
            self.assertEqual(set(rej.Reason), {expected})
        return valid

    def test_clean_rows_pass_and_volume_is_int(self):
        valid = self.check(bars("A", ["2026-09-28", "2026-09-29"]), None)
        self.assertEqual(str(valid.Volume.dtype), "int64")

    def test_empty_frame(self):
        valid, rej = validate(bars("A", []), self.cal)
        self.assertTrue(valid.empty and rej.empty)

    def test_schema_bad_date(self):
        self.check(bars("A", ["28/09/2026"]), "SCHEMA")

    def test_schema_non_numeric_price(self):
        self.check(bars("A", ["2026-09-28"], Close="abc"), "SCHEMA")

    def test_schema_empty_ticker(self):
        self.check(bars("", ["2026-09-28"]), "SCHEMA")

    def test_schema_fractional_volume(self):
        self.check(bars("A", ["2026-09-28"], Volume=10.5), "SCHEMA")

    def test_schema_missing_column(self):
        _, rej = validate(bars("A", ["2026-09-28"]).drop(columns=["Volume"]), self.cal)
        self.assertEqual(set(rej.Reason), {"SCHEMA"})

    def test_ohlc_high_below_close(self):
        self.check(bars("A", ["2026-09-28"], High=99.0), "OHLC")

    def test_ohlc_low_above_open(self):
        self.check(bars("A", ["2026-09-28"], Low=100.5), "OHLC")

    def test_ohlc_negative_volume(self):
        self.check(bars("A", ["2026-09-28"], Volume=-1), "OHLC")

    def test_zero_volume_allowed(self):
        self.check(bars("A", ["2026-09-28"], Volume=0), None)

    def test_duplicates_in_batch_reject_every_copy(self):
        valid, rej = validate(bars("A", ["2026-09-28", "2026-09-28", "2026-09-29"]), self.cal)
        self.assertEqual(list(rej.Reason), ["DUP_IN_BATCH"] * 2)
        self.assertEqual(list(valid.Date), ["2026-09-29"])

    def test_same_date_different_tickers_is_not_a_duplicate(self):
        self.check(pd.concat([bars("A", ["2026-09-28"]), bars("B", ["2026-09-28"])]), None)

    def test_weekend_rejected(self):
        self.check(bars("A", ["2026-09-26"]), "NON_TRADING_DAY")

    def test_holiday_rejected_and_special_session_allowed(self):
        self.set_calendar(holidays=["2026-09-28"], special=["2026-09-26"])
        self.cal = Calendar(self.cfg["paths"]["calendar"])
        valid, rej = validate(bars("A", ["2026-09-28", "2026-09-26"]), self.cal)
        self.assertEqual(list(rej.Date), ["2026-09-28"])
        self.assertEqual(list(valid.Date), ["2026-09-26"])

    def test_null_value_flagged(self):
        df = bars("A", ["2026-09-28", "2026-09-29"])
        df.loc[0, "Close"] = None
        valid, rej = validate(df, self.cal)
        self.assertEqual(list(rej.Reason), ["NULL_VALUE"])
        self.assertEqual(len(valid), 1)

    def test_rule_priority_schema_beats_null_beats_ohlc(self):
        df = bars("A", ["2026-09-28"], Open=None, High=1.0)
        self.assertEqual(set(validate(df, self.cal)[1].Reason), {"NULL_VALUE"})


class CalendarTests(Env):
    def cal(self, **kw) -> Calendar:
        self.set_calendar(**kw)
        return Calendar(self.cfg["paths"]["calendar"])

    def test_weekend_holiday_special(self):
        cal = self.cal(holidays=["2026-09-28"], special=["2026-10-03"])
        self.assertTrue(cal.is_trading_day("2026-09-29"))
        self.assertFalse(cal.is_trading_day("2026-09-27"))
        self.assertFalse(cal.is_trading_day("2026-09-28"))
        self.assertTrue(cal.is_trading_day("2026-10-03"))

    def test_days_range(self):
        days = self.cal().days(date(2026, 9, 25), date(2026, 9, 29))
        self.assertEqual([d.isoformat() for d in days], ["2026-09-25", "2026-09-28", "2026-09-29"])

    def test_final_session_today_after_cutoff(self):
        self.assertEqual(self.cal().last_final_session(NOW, "20:00"), date(2026, 9, 29))

    def test_final_session_today_before_cutoff_uses_previous_day(self):
        early = datetime(2026, 9, 29, 19, 59, tzinfo=IST)
        self.assertEqual(self.cal().last_final_session(early, "20:00"), date(2026, 9, 28))

    def test_final_session_skips_weekend_and_holiday(self):
        cal = self.cal(holidays=["2026-09-25"])
        monday_morning = datetime(2026, 9, 28, 9, 0, tzinfo=IST)
        self.assertEqual(cal.last_final_session(monday_morning, "20:00"), date(2026, 9, 24))

    def test_final_session_on_holiday_evening(self):
        cal = self.cal(holidays=["2026-09-29"])
        self.assertEqual(cal.last_final_session(NOW, "20:00"), date(2026, 9, 28))


class CommonTests(Env):
    def test_lock_is_exclusive_and_released(self):
        with common.run_lock(self.cfg, LOG):
            self.assertTrue((self.market / ".lock").exists())
            with self.assertRaises(Busy), common.run_lock(self.cfg, LOG):
                pass
        self.assertFalse((self.market / ".lock").exists())

    def test_lock_released_after_error(self):
        with self.assertRaises(ValueError), common.run_lock(self.cfg, LOG):
            raise ValueError
        self.assertFalse((self.market / ".lock").exists())

    def test_stale_lock_is_taken_over(self):
        self.market.mkdir(parents=True)
        lock = self.market / ".lock"
        lock.write_text("1 old")
        os.utime(lock, (0, 0))
        with common.run_lock(self.cfg, LOG):
            self.assertNotEqual(lock.read_text(), "1 old")

    def test_fresh_lock_is_not_taken_over(self):
        self.market.mkdir(parents=True)
        (self.market / ".lock").write_text("1 recent")
        with self.assertRaises(Busy), common.run_lock(self.cfg, LOG):
            pass

    def test_atomic_leaves_no_temp_file(self):
        target = self.root / "x" / "f.csv"
        common.write_csv(pd.DataFrame({"a": [1]}), target)
        self.assertEqual([p.name for p in target.parent.iterdir()], ["f.csv"])

    def test_report_status_and_summary(self):
        r = Report("updator")
        self.assertEqual(r.status(), "ok")
        r.failed["X"] = "boom"
        self.assertEqual(r.status(), "partial")
        r.error = "fatal"
        s = r.summary(NOW)
        self.assertEqual(s["status"], "failed")
        self.assertEqual(s["tickersFailed"], ["X"])
        self.assertEqual(s["runDate"], TODAY)
        self.assertEqual(s["rowsRejected"], 0)

    def test_digest_lists_everything(self):
        r = Report("updator")
        r.rejects.append(pd.DataFrame({"Ticker": ["A", "A", "B"], "Date": "d", "Reason": ["OHLC", "OHLC", "SCHEMA"]}))
        r.failed["F"] = "fetch failed"
        r.inactive.append("I")
        r.rebuilt["S"] = "split 2026-09-29"
        r.notes.append("registry not refreshed")
        r.error = "kaboom"
        subject, body = r.digest(NOW)
        self.assertIn("FAILED", subject)
        for text in ("OHLC 2", "SCHEMA 1", "A (2)", "F: fetch failed", "I", "S: split", "registry not refreshed", "kaboom"):
            self.assertIn(text, body)

    def run_stage(self, run, expect_exit: int | None):
        cfg_file = self.root / "market.json"
        cfg_file.write_text(json.dumps(self.cfg))
        with patch.dict(os.environ, {"MARKET_CONFIG_PATH": str(cfg_file)}), patch("app.market.mailer.send") as send:
            if expect_exit is None:
                common.run_stage("updator", run)
            else:
                with self.assertRaises(SystemExit) as cm:
                    common.run_stage("updator", run)
                self.assertEqual(cm.exception.code, expect_exit)
        return send

    def test_run_stage_writes_status_rejects_digest(self):
        def run(cfg, now, log, report):
            report.updated.add("A")
            report.last_trading_day = "2026-09-29"
            report.rejects.append(pd.DataFrame({"Ticker": ["A"], "Date": ["2026-09-28"], "Reason": ["OHLC"]}))

        send = self.run_stage(run, None)
        status = json.loads((self.market / "status.json").read_text())
        self.assertEqual((status["status"], status["tickersUpdated"], status["rowsRejected"]), ("ok", 1, 1))
        self.assertEqual(len(list((self.root / "logs" / "rejects").glob("*_updator.csv"))), 1)
        send.assert_called_once()
        self.assertFalse((self.market / ".lock").exists())

    def test_run_stage_failure_exits_1_with_failed_status(self):
        def run(cfg, now, log, report):
            raise RuntimeError("bad")

        send = self.run_stage(run, 1)
        self.assertEqual(json.loads((self.market / "status.json").read_text())["status"], "failed")
        send.assert_called_once()
        self.assertFalse((self.market / ".lock").exists())

    def test_run_stage_busy_exits_2_and_keeps_other_lock(self):
        self.market.mkdir(parents=True)
        (self.market / ".lock").write_text("other")
        ran = MagicMock()
        self.run_stage(ran, 2)
        ran.assert_not_called()
        self.assertTrue((self.market / ".lock").exists())

    def test_run_stage_quiet_skips_status(self):
        def run(cfg, now, log, report):
            report.quiet = True

        self.run_stage(run, None)
        self.assertFalse((self.market / "status.json").exists())


class MailerTests(Env):
    def test_unconfigured_is_skipped(self):
        self.cfg["mail"]["smtpHost"] = "<set at deployment>"
        self.assertFalse(mailer.send(self.cfg, "s", "b", LOG))

    def test_placeholder_recipients_skipped(self):
        self.cfg["mail"]["recipients"] = ["<set at deployment>"]
        self.assertFalse(mailer.send(self.cfg, "s", "b", LOG))

    def test_sends_with_starttls_and_env_credentials(self):
        with patch("app.market.mailer.smtplib.SMTP") as smtp, patch.dict(os.environ, {"SMTP_USER": "u", "SMTP_PASSWORD": "p"}):
            self.assertTrue(mailer.send(self.cfg, "subj", "body", LOG))
        server = smtp.return_value.__enter__.return_value
        server.starttls.assert_called_once()
        server.login.assert_called_once_with("u", "p")
        self.assertEqual(server.send_message.call_args.args[0]["Subject"], "subj")

    def test_send_failure_is_swallowed(self):
        with patch("app.market.mailer.smtplib.SMTP", side_effect=smtplib.SMTPException("down")):
            self.assertFalse(mailer.send(self.cfg, "s", "b", LOG))


class RegistryTests(Env):
    def test_missing_registry_is_empty(self):
        self.assertTrue(self.registry().empty)

    def test_upstream_symbols_takes_newest_file_date(self):
        self.write_upstream(["AAA", "BBB"], "2026-09-28")
        self.write_upstream(["AAA"], "2026-09-29")
        self.assertEqual(registry.upstream_symbols(self.cfg), {"AAA": "2026-09-29", "BBB": "2026-09-28"})

    def test_refresh_appends_new_and_roundtrips(self):
        reg = self.registry()
        registry.refresh(reg, {"AAA": "2026-09-29", "M&M": "2026-09-29"})
        registry.save(reg, self.market / "registry.csv")
        back = self.registry()
        self.assertEqual(list(back.index), ["AAA", "M&M"])
        self.assertEqual(back.at["M&M", "yahoo_symbol"], "M&M.NS")
        self.assertEqual((back.at["AAA", "status"], back.at["AAA", "no_data_days"], back.at["AAA", "first_seen"]), ("active", 0, "2026-09-29"))

    def test_refresh_updates_last_seen_keeps_first_seen(self):
        reg = self.registry()
        registry.refresh(reg, {"AAA": "2026-09-01"})
        registry.refresh(reg, {"AAA": "2026-09-29"})
        self.assertEqual((reg.at["AAA", "first_seen"], reg.at["AAA", "last_seen_upstream"]), ("2026-09-01", "2026-09-29"))

    def test_symbols_never_deleted(self):
        reg = self.registry()
        registry.refresh(reg, {"AAA": "2026-09-01", "BBB": "2026-09-01"})
        registry.refresh(reg, {"AAA": "2026-09-29"})
        self.assertIn("BBB", reg.index)

    def inactive_registry(self) -> pd.DataFrame:
        reg = self.registry()
        registry.refresh(reg, {"AAA": "2026-09-01"})
        reg.loc["AAA", ["status", "inactive_since"]] = ["inactive", "2026-09-10"]
        return reg

    def test_inactive_but_still_listed_stays_inactive(self):
        reg = self.inactive_registry()
        self.assertEqual(registry.refresh(reg, {"AAA": "2026-09-29"}), [])
        self.assertEqual(reg.at["AAA", "status"], "inactive")

    def test_absent_then_present_reactivates(self):
        reg = self.inactive_registry()
        registry.refresh(reg, {"ZZZ": "2026-09-20"})
        self.assertTrue(reg.at["AAA", "absent_since_inactive"])
        self.assertEqual(registry.refresh(reg, {"AAA": "2026-09-29"}), ["AAA"])
        self.assertEqual((reg.at["AAA", "status"], reg.at["AAA", "no_data_days"], reg.at["AAA", "inactive_since"]), ("active", 0, ""))
        self.assertFalse(reg.at["AAA", "absent_since_inactive"])

    def test_upstream_ok_healthy_today(self):
        self.write_upstream(["A"])
        self.assertTrue(registry.upstream_ok(self.cfg, TODAY))

    def test_upstream_ok_rejects_stale_unhealthy_missing_corrupt(self):
        self.write_upstream(["A"], checked="2026-09-28T19:45:00+05:30")
        self.assertFalse(registry.upstream_ok(self.cfg, TODAY))
        self.write_upstream(["A"], healthy=False)
        self.assertFalse(registry.upstream_ok(self.cfg, TODAY))
        Path(self.cfg["paths"]["upstreamHealth"]).write_text("{not json")
        self.assertFalse(registry.upstream_ok(self.cfg, TODAY))
        Path(self.cfg["paths"]["upstreamHealth"]).unlink()
        self.assertFalse(registry.upstream_ok(self.cfg, TODAY))

    def test_checked_at_is_compared_in_ist(self):
        # 20:00 UTC on the 28th is 01:30 IST on the 29th
        self.write_upstream(["A"], checked="2026-09-28T20:00:00+00:00")
        self.assertTrue(registry.upstream_ok(self.cfg, TODAY))
