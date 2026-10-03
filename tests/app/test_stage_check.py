"""`--check` on the batch stages: validates config/calendar/dependencies, touches nothing."""

import contextlib
import io
import os
import unittest
from unittest.mock import patch

from app.market import common as market_common
from app.metadata import common as metadata_common


class StageCheckTests(unittest.TestCase):
    def run_check(self, module, argv):
        ran = []
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as cm:
                module.run_stage("stage", lambda *a: ran.append(a), argv=argv)
        return cm.exception.code, ran, out.getvalue(), err.getvalue()

    def test_market_check_passes_without_running_the_stage_or_writing(self):
        with patch.object(market_common, "setup_logging", side_effect=AssertionError("no log files")), patch.object(
            market_common, "run_lock", side_effect=AssertionError("no lock")
        ):
            code, ran, out, _ = self.run_check(market_common, ["--check"])
        self.assertEqual((code, ran), (0, []))
        self.assertIn("check ok", out)

    def test_market_check_fails_on_bad_config(self):
        with patch.object(market_common, "CONFIG_PATH", "app/config/does_not_exist.json"):
            code, ran, _, err = self.run_check(market_common, ["--check"])
        self.assertEqual((code, ran), (1, []))
        self.assertIn("check FAILED", err)

    def test_metadata_check_passes_without_running_the_stage_or_writing(self):
        with patch.object(metadata_common, "setup_logging", side_effect=AssertionError("no log files")):
            code, ran, out, _ = self.run_check(metadata_common, ["--check"])
        self.assertEqual((code, ran), (0, []))
        self.assertIn("check ok", out)

    def test_metadata_check_fails_on_bad_config(self):
        with patch.dict(os.environ, {"CONFIG_PATH": "app/config/does_not_exist.json"}):
            code, ran, _, err = self.run_check(metadata_common, ["--check"])
        self.assertEqual((code, ran), (1, []))
        self.assertIn("check FAILED", err)

    def test_without_the_flag_the_stage_still_runs(self):
        ran = []
        with patch.object(metadata_common, "load_config", return_value={"paths": {"health": "x/h.json"}}), patch.object(
            metadata_common, "setup_logging"
        ):
            metadata_common.run_stage("stage", lambda *a: ran.append(a), argv=[])
        self.assertEqual(len(ran), 1)


if __name__ == "__main__":
    unittest.main()
