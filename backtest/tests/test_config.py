import copy
import io
import unittest
from contextlib import redirect_stderr, redirect_stdout

from backtest import config, run


def cfg() -> dict:
    return copy.deepcopy(config.load())


class ConfigTests(unittest.TestCase):
    def bad(self, edit):
        c = cfg()
        edit(c)
        with self.assertRaises(ValueError):
            config.validate(c)

    def test_shipped_config_is_valid(self):
        config.validate(cfg())

    def test_purge_cannot_be_below_168_days(self):
        self.bad(lambda c: c["walkforward"].update(purgeDays=167))

    def test_gate_values_are_bounded(self):
        self.bad(lambda c: c["gate"].update(pboMax=1.5))
        self.bad(lambda c: c["gate"].update(dsrMin="0.95"))

    def test_tax_schedule_must_ascend(self):
        self.bad(lambda c: c["tax"]["schedule"].reverse())

    def test_composition_and_window(self):
        self.bad(lambda c: c["capital"]["composition"].update(LargeCap=0.9))
        self.bad(lambda c: c["window"].update(start="2008-13-01"))
        self.bad(lambda c: c["window"].update(holdoutYears=0))

    def test_stress_windows_and_compute(self):
        self.bad(lambda c: c["stress"].update(x=[["2020-02-01", "2019-01-01"]]))
        self.bad(lambda c: c["compute"].update(workers=9))

    def test_unsupported_fill_mode(self):
        self.bad(lambda c: c["fill"].update(mode="vwap"))


class CheckTests(unittest.TestCase):
    def test_check_ok_and_writes_nothing(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = run.main(["--check"])
        self.assertEqual(code, 0, err.getvalue())
        self.assertIn("check ok", out.getvalue())
