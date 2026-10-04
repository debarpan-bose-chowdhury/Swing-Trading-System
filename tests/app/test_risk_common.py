"""Config validation (the TDD table), --check on every stage, run_stage exit codes, lock, status and calendar helpers."""

import contextlib
import copy
import io
import json
import os
import time
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

from app.market.common import IST
from app.risk import common
from app.risk.common import Gate, Report
from tests.app.risk_helpers import Env


class ShippedConfigTests(unittest.TestCase):
    """Run in the repository root: the config that ships in the image."""

    def test_shipped_config_loads_for_run_evaluate_and_surveillance(self):
        cfg = common.load_config("run")
        self.assertEqual(cfg["buckets"], ["LargeCap", "MidCap", "SmallCap"])
        self.assertIn("brokerage", cfg["costs"])  # the Analyst's cost model, read-only
        common.load_config("evaluate")
        common.load_config("surveillance")  # sources are now pre-configured with correct object format

    def test_every_stage_check(self):
        for stage, expected in (("run", 0), ("evaluate", 0), ("surveillance", 0)):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = common.check_stage(stage)
            self.assertEqual(code, expected, stage)
            self.assertIn("check ok" if expected == 0 else "check FAILED", out.getvalue() + err.getvalue())

    def test_check_flag_runs_no_stage_and_writes_nothing(self):
        ran = []
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), patch.object(common, "setup_logging", side_effect=AssertionError("no log")), \
                patch.object(common, "run_lock", side_effect=AssertionError("no lock")), self.assertRaises(SystemExit) as cm:
            common.run_stage("run", lambda *a: ran.append(a), argv=["--check"])
        self.assertEqual((cm.exception.code, ran), (0, []))

    def test_check_fails_on_a_missing_config(self):
        with patch.object(common, "CONFIG_PATH", "app/config/missing.json"), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(common.check_stage("run"), 1)

    def test_the_shipped_cash_flow_template_is_header_only(self):
        self.assertEqual(Path("app/config/cash_flows.csv").read_text().strip(), "date,type,amount_inr,note")

    def test_the_shipped_defaults_match_the_tdd(self):
        c = json.loads(Path("app/config/risk.json").read_text())
        self.assertEqual((c["sizing"]["riskPerPositionPct"], c["sizing"]["nameCapPct"], c["stops"]["atrMultiplier"], c["heat"]["capPct"]),
                         (0.0125, {"LargeCap": 0.10, "MidCap": 0.08, "SmallCap": 0.06}, 3.5, 0.12))
        self.assertEqual([(lv["drawdownPct"], lv["maxInvestedPct"]) for lv in c["ladder"]["levels"]], [(0.10, 0.75), (0.15, 0.5), (0.20, 0.25), (0.25, 0.0)])
        self.assertEqual(set(c["exposure"]["regimeCap"].values()), {1.0})
        self.assertFalse(c["placeholders"])
        self.assertEqual(c["stops"]["clampPct"], {"LargeCap": [0.10, 0.18], "MidCap": [0.14, 0.22], "SmallCap": [0.18, 0.28]})


class ValidationTests(Env):
    def bad(self, mutate, message, stage="run"):
        cfg = copy.deepcopy(self.cfg)
        mutate(cfg)
        with self.assertRaisesRegex(ValueError, message):
            common.validate(cfg, stage)

    def test_valid_config_passes(self):
        common.validate(copy.deepcopy(self.cfg), "run")

    def test_placeholders_block_run_only(self):
        self.bad(lambda c: c.update(placeholders=True), "placeholders", "run")
        cfg = copy.deepcopy(self.cfg)
        cfg["placeholders"] = True
        common.validate(cfg, "evaluate")

    def test_sizing(self):
        self.bad(lambda c: c["sizing"].update(riskPerPositionPct=0), "sizing")
        self.bad(lambda c: c["sizing"].update(cashBufferPct=1.5), "sizing")
        self.bad(lambda c: c["sizing"].update(minNewOrderInr=0), "sizing")
        self.bad(lambda c: c["sizing"]["nameCapPct"].update(SmallCap=0), "sizing")
        self.bad(lambda c: c["sizing"]["nameCapPct"].pop("SmallCap"), "nameCapPct needs an entry")  # a bucket of the Analyst's composition

    def test_stops(self):
        self.bad(lambda c: c["stops"].update(atrPeriod=1), "atrPeriod")
        self.bad(lambda c: c["stops"].update(atrMultiplier=0), "atrMultiplier")
        self.bad(lambda c: c["stops"].update(bucketFallback="NanoCap"), "bucketFallback")
        self.bad(lambda c: c["stops"]["clampPct"].update(SmallCap=[0.3, 0.2]), "clampPct")
        self.bad(lambda c: c["stops"]["clampPct"].update(SmallCap=[0.1, 1.0]), "clampPct")
        self.bad(lambda c: c["stops"]["clampPct"].pop("MidCap"), "clampPct")

    def test_ladder(self):
        self.bad(lambda c: c["ladder"].update(levels=[]), "ladder.levels")
        self.bad(lambda c: c["ladder"].update(levels=[{"drawdownPct": 0.2, "maxInvestedPct": 0.5}, {"drawdownPct": 0.1, "maxInvestedPct": 0.2}]), "ladder.levels")  # not ascending
        self.bad(lambda c: c["ladder"].update(levels=[{"drawdownPct": 0.1, "maxInvestedPct": 0.2}, {"drawdownPct": 0.2, "maxInvestedPct": 0.5}]), "ladder.levels")  # not descending
        self.bad(lambda c: c["ladder"].update(levels=[{"drawdownPct": 0.1 * i, "maxInvestedPct": 0.9 - 0.1 * i} for i in range(1, 8)]), "ladder.levels")  # 7 levels
        self.bad(lambda c: c["ladder"].update(levels=[{"drawdownPct": 0.1, "maxInvestedPct": 0.0}, {"drawdownPct": 0.2, "maxInvestedPct": 0.0}]), "ladder.levels")  # only the last may be 0
        self.bad(lambda c: c["ladder"].update(restartFrom="soon"), "restartFrom")
        self.bad(lambda c: c["ladder"]["reRisk"].update(consecutiveWeeks=0), "reRisk")
        cfg = copy.deepcopy(self.cfg)
        cfg["ladder"]["restartFrom"] = "2026-10-05"
        common.validate(cfg, "run")

    def test_exposure_heat_and_liquidity(self):
        self.bad(lambda c: c["exposure"]["regimeCap"].update(SIDEWAYS=1.0), "exposure.regimeCap")
        self.bad(lambda c: c["exposure"]["regimeCap"].update(BULL=1.5), "exposure.regimeCap")
        self.bad(lambda c: c["heat"].update(capPct=0), "heat")
        self.bad(lambda c: c["liquidity"]["maxParticipationPct"].update(SmallCap=2), "liquidity")
        self.bad(lambda c: c["liquidity"].update(advDays=0), "liquidity")

    def test_surveillance_sources_are_only_required_by_the_surveillance_stage(self):
        cfg = copy.deepcopy(self.cfg)
        cfg["surveillance"]["sources"] = dict.fromkeys(("asm", "gsm", "t2t", "bands"), "<set after probe>")
        common.validate(cfg, "run")
        common.validate(cfg, "evaluate")
        with self.assertRaisesRegex(ValueError, "surveillance.sources.asm"):
            common.validate(cfg, "surveillance")
        cfg["surveillance"]["sources"] = {n: {"url": f"https://x/{n}", "format": "csv", "symbolColumn": "S"} for n in ("asm", "gsm", "t2t", "bands")}
        common.validate(cfg, "surveillance")
        cfg["surveillance"]["sources"]["gsm"]["format"] = "xml"
        with self.assertRaisesRegex(ValueError, "surveillance.sources.gsm"):
            common.validate(cfg, "surveillance")

    def test_surveillance_rules(self):
        self.bad(lambda c: c["surveillance"].update(staleExitDays=-1), "staleExitDays")
        self.bad(lambda c: c["surveillance"].update(exitOn=["ASM"]), "exitOn")

    def test_tax(self):
        self.bad(lambda c: c["tax"]["rates"].update(stcgPct=-0.1), "tax")
        self.bad(lambda c: c["tax"]["deferral"].update(windowDays=0), "tax")

    def test_gate(self):
        self.bad(lambda c: c["gate"].update(maxMissingShare=1.5), "gate")
        self.bad(lambda c: c["gate"].update(targetsWaitUntil="Someday 22:00"), "gate")
        self.bad(lambda c: c["gate"].update(targetsWaitUntil="22:00"), "gate")
        self.bad(lambda c: c["gate"].update(maxLedgerLagTradingDays=-1), "gate")

    def test_evaluator(self):
        self.bad(lambda c: c["evaluator"].update(benchmark="^NSEBANK"), "benchmark")  # not in this tree's indices.json
        self.bad(lambda c: c["evaluator"].update(riskFreeRatePct=-1), "riskFreeRatePct")


class RunStageTests(Env):
    """run_stage: exit codes, lock, status block and digest."""

    def setUp(self):
        super().setUp()
        Path("config/risk.json").write_text(json.dumps(self.cfg))
        self.mails = []
        for patcher in (patch.object(common, "CONFIG_PATH", "config/risk.json"), patch("app.market.mailer.send", side_effect=self.mail)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def mail(self, cfg, subject, body, log):
        self.mails.append((subject, body))

    def stage(self, fn, argv=()):
        with self.assertRaises(SystemExit) as cm:
            common.run_stage("run", fn, argv=list(argv))
        return cm.exception.code

    def status(self):
        return json.loads((self.risk / "risk_status.json").read_text())

    def test_ok_run_writes_status_sends_digest_and_exits_normally(self):
        def fn(cfg, now, log, report, args):
            report.block.update(asOf="2026-09-25", signals=3)
            report.lines.append("hello")
        common.run_stage("run", fn, argv=[])
        block = self.status()["run"]
        self.assertEqual((block["status"], block["asOf"], block["signals"]), ("ok", "2026-09-25", 3))
        self.assertTrue(self.mails[0][0].startswith("[Risk] run OK"))
        self.assertIn("hello", self.mails[0][1])
        self.assertFalse((self.risk / ".lock").exists())

    def test_failure_exits_1_with_a_failed_status_and_digest(self):
        def fn(*a):
            raise RuntimeError("boom")
        self.assertEqual(self.stage(fn), 1)
        self.assertEqual(self.status()["run"]["status"], "failed")
        self.assertIn("FATAL: boom", self.mails[0][1])
        self.assertFalse((self.risk / ".lock").exists())

    def test_gate_exits_3_without_status_or_email(self):
        def fn(*a):
            raise Gate("not yet")
        self.assertEqual(self.stage(fn), 3)
        self.assertEqual((self.mails, (self.risk / "risk_status.json").exists()), ([], False))
        self.assertFalse((self.risk / ".lock").exists())

    def test_busy_exits_2_and_leaves_the_other_lock_alone(self):
        self.risk.mkdir(parents=True, exist_ok=True)
        (self.risk / ".lock").write_text("123 now")
        self.assertEqual(self.stage(lambda *a: None), 2)
        self.assertTrue((self.risk / ".lock").exists())

    def test_a_stale_lock_is_taken_over(self):
        self.risk.mkdir(parents=True, exist_ok=True)
        lock = self.risk / ".lock"
        lock.write_text("123 old")
        old = time.time() - 7 * 3600
        os.utime(lock, (old, old))
        ran = []
        common.run_stage("run", lambda *a: ran.append(1), argv=[])
        self.assertEqual(ran, [1])
        self.assertFalse(lock.exists())

    def test_a_quiet_run_writes_nothing(self):
        def fn(cfg, now, log, report, args):
            report.quiet = True
        common.run_stage("run", fn, argv=[])
        self.assertEqual((self.mails, (self.risk / "risk_status.json").exists()), ([], False))

    def test_invalid_config_exits_1_with_a_digest(self):
        bad = copy.deepcopy(self.cfg)
        bad["placeholders"] = True
        Path("config/risk.json").write_text(json.dumps(bad))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.stage(lambda *a: None), 1)
        self.assertIn("placeholders", self.mails[0][1])

    def test_force_flag_reaches_the_stage(self):
        seen = []
        common.run_stage("run", lambda cfg, now, log, report, args: seen.append(args.force), argv=["--force"])
        self.assertEqual(seen, [True])

    def test_status_blocks_of_the_stages_coexist(self):
        for stage in ("surveillance", "run", "evaluate"):
            r = Report(stage)
            r.block["x"] = stage
            common.write_status(self.cfg, r, datetime(2026, 9, 25, 21, 45, tzinfo=IST))
        self.assertEqual(set(self.status()), {"surveillance", "run", "evaluate"})

    def test_corrupt_ladder_state_fails_the_run(self):
        (self.risk / "state").mkdir(parents=True)
        (self.risk / "state/ladder_state.json").write_text("{broken")
        with self.assertRaisesRegex(ValueError, "corrupt"):
            common.load_state(self.risk / "state")


class DateHelperTests(Env):
    def test_trading_day_arithmetic(self):
        cal = self.context().cal
        self.assertEqual(common.next_trading_day(cal, date(2026, 10, 1)), date(2026, 10, 5))  # 2 Oct holiday, then the weekend
        self.assertEqual(common.add_trading_days(cal, date(2026, 9, 24), 10), date(2026, 10, 9))
        self.assertEqual(common.trading_days_between(cal, "2026-09-21", "2026-09-25"), 4)
        self.assertEqual(common.trading_days_between(cal, "2026-09-25", "2026-09-25"), 0)
        self.assertEqual(common.trading_days_between(cal, "2026-09-26", "2026-09-25"), 0)

    def test_anniversary_and_iso_week(self):
        self.assertEqual(common.anniversary(date(2025, 10, 10)), date(2026, 10, 10))
        self.assertEqual(common.anniversary(date(2024, 2, 29)), date(2025, 2, 28))
        self.assertEqual(common.iso_week("2026-09-25"), "2026-W39")
        self.assertEqual(common.iso_week("2026-01-01"), "2026-W01")


if __name__ == "__main__":
    unittest.main()
