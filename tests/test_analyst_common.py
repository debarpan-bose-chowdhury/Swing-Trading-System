"""Analyst config validation, cost model (TDD worked example), run lock, status file, --check."""

import contextlib
import io
import json
import logging
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from app.analyst import common, costs
from app.market.common import IST

LOG = logging.getLogger("test.analyst")
NOW = datetime(2026, 10, 2, 21, 30, tzinfo=IST)


def cfg() -> dict:
    return common.load_config()


class ValidationTests(unittest.TestCase):
    def bad(self, mutate):
        c = cfg()
        mutate(c)
        with self.assertRaises(ValueError):
            common.validate(c)

    def test_shipped_config_is_valid(self):
        common.validate(cfg())

    def test_composition_rules(self):
        self.bad(lambda c: c["composition"].update(LargeCap=0.9))
        self.bad(lambda c: c["composition"].update(Mystery=0.0))

    def test_limits_regime_and_strategies(self):
        self.bad(lambda c: c["limits"].update(maxPositionDrawdownPct=0))
        self.bad(lambda c: c["limits"].update(maxPortfolioDrawdownPct=1.5))
        self.bad(lambda c: c["regime"].update(persistenceWeeks=0))
        self.bad(lambda c: c["strategies"].pop("BEAR"))
        self.bad(lambda c: c["strategies"]["BULL"].pop("MidCap"))
        self.bad(lambda c: c["strategies"]["BULL"]["MidCap"].update(top_n=-1))
        self.bad(lambda c: c["strategies"]["BULL"]["MidCap"].update(lookback=0))

    def test_signals_selector_and_regime_numbers_are_checked_at_load(self):
        self.bad(lambda c: c["selector"].update(maxMissingShare="0.1"))
        self.bad(lambda c: c["selector"].update(maxMissingShare=1.5))
        self.bad(lambda c: c["selector"].update(maxBucketFileAgeDays=-1))
        self.bad(lambda c: c["selector"].update(maxStaleTradingDays=1))
        self.bad(lambda c: c["selector"].update(momentumSkipDays=-1))
        self.bad(lambda c: c["selector"]["liquidity"].update(statistic="max"))
        self.bad(lambda c: c["selector"]["bearScore"].pop("dd63"))
        self.bad(lambda c: c["regime"].update(minRows=0))
        self.bad(lambda c: c["signals"].update(retryEveryMinutes=0))
        self.bad(lambda c: c["signals"].update(retryUntil="Sunday 10pm"))

    def test_rebalance_capital_and_costs(self):
        self.bad(lambda c: c["rebalance"].update(schedule="daily"))
        self.bad(lambda c: c["capital"].update(floatingCapitalInr=-1))
        self.bad(lambda c: c["costs"].update(sttPct=-0.1))
        self.bad(lambda c: c["costs"]["brokerage"].update(pct="x"))


class CostTests(unittest.TestCase):
    def test_worked_example_one_lakh_round_trip(self):
        c = cfg()["costs"]
        self.assertAlmostEqual(costs.buy_charges(c, 100000), 142.34, places=2)
        self.assertAlmostEqual(costs.sell_charges(c, 100000), 150.94, places=2)
        self.assertAlmostEqual(costs.round_trip({**c, "slippageBpsPerSide": {}}, "LargeCap", 100000), 293.28, places=2)

    def test_slippage_is_per_side_and_per_bucket(self):
        c = cfg()["costs"]
        base = costs.round_trip(c, "Other", 100000)
        self.assertAlmostEqual(costs.round_trip(c, "SmallCap", 100000) - base, 2 * 50 / 10000 * 100000)

    def test_brokerage_floor_and_exit_cost(self):
        c = cfg()["costs"]
        self.assertGreater(costs.buy_charges(c, 100), 5)  # minimum brokerage applies on tiny orders
        self.assertAlmostEqual(costs.exit_cost(c, "LargeCap", 10, 10000), costs.sell_charges(c, 100000) + 100.0)
        self.assertAlmostEqual(costs.exit_cost(c, None, 10, 10000), costs.sell_charges(c, 100000))


class RunStageTests(unittest.TestCase):
    def setUp(self):
        self._cwd, self.tmp = os.getcwd(), tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(os.chdir, self._cwd)
        root = Path(self.tmp.name)
        (root / "app/config").mkdir(parents=True)
        for f in ("analyst.json", "config.json", "nse_calendar.json"):
            (root / "app/config" / f).write_text((Path(self._cwd) / "app/config" / f).read_text(encoding="utf-8"), encoding="utf-8")
        os.chdir(root)
        self.root = root

    def run_stage(self, run, argv=()):
        with patch("app.analyst.common.datetime") as dt, patch("app.market.mailer.send") as send:
            dt.now.return_value = NOW
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as cm:
                common.run_stage("signals", run, argv=list(argv))
                raise SystemExit(0)
        return cm.exception.code, send

    def status(self) -> dict:
        return json.loads((self.root / "app/data/analyst/analyst_status.json").read_text(encoding="utf-8"))

    def test_check_touches_nothing(self):
        with patch.object(common, "setup_logging", side_effect=AssertionError("no log")), patch.object(
            common, "run_lock", side_effect=AssertionError("no lock")
        ):
            code, _ = self.run_stage(lambda *a: None, ["--check"])
        self.assertEqual(code, 0)
        self.assertFalse((self.root / "app/data").exists())

    def test_check_fails_on_bad_config(self):
        with patch.object(common, "CONFIG_PATH", "app/config/missing.json"):
            code, _ = self.run_stage(lambda *a: None, ["--check"])
        self.assertEqual(code, 1)

    def test_success_writes_status_and_sends_digest(self):
        def run(c, now, log, report, args):
            report.block["selectedCount"] = 3
            report.lines.append("hello")

        code, send = self.run_stage(run)
        self.assertEqual(code, 0)
        block = self.status()["signals"]
        self.assertEqual((block["status"], block["selectedCount"], block["lastGoodRunDate"]), ("ok", 3, "2026-10-02"))
        self.assertIn("hello", send.call_args.args[2])
        self.assertFalse((self.root / "app/data/analyst/.lock").exists())

    def test_failure_exits_1_and_keeps_last_good_date(self):
        self.run_stage(lambda *a: None)
        def boom(*a):
            raise RuntimeError("x")
        code, _ = self.run_stage(boom)
        block = self.status()["signals"]
        self.assertEqual((code, block["status"], block["lastGoodRunDate"]), (1, "failed", "2026-10-02"))

    def test_gate_exits_3_without_status_or_mail(self):
        def gate(*a):
            raise common.Gate("not yet")
        code, send = self.run_stage(gate)
        self.assertEqual(code, 3)
        send.assert_not_called()
        self.assertFalse((self.root / "app/data/analyst/analyst_status.json").exists())

    def test_quiet_run_writes_nothing(self):
        def run(c, now, log, report, args):
            report.quiet = True
        code, send = self.run_stage(run)
        self.assertEqual(code, 0)
        send.assert_not_called()

    def test_busy_exits_2_and_stale_lock_is_taken_over(self):
        lock = self.root / "app/data/analyst/.lock"
        lock.parent.mkdir(parents=True)
        lock.write_text("1 x")
        code, _ = self.run_stage(lambda *a: None)
        self.assertEqual(code, 2)
        old = os.stat(lock).st_mtime - 7 * 3600
        os.utime(lock, (old, old))
        code, _ = self.run_stage(lambda *a: None)
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
