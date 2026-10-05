"""Default parity (Parameter Exposure S1): with the shipped configs a synthetic replay is byte-identical to the one recorded before S1.

Regenerate only on purpose, with GOLDEN_WRITE=1 (a diff in tests/backtest/golden.json then needs a reason in the commit).
tests/backtest/golden.json holds sha256 digests of the NAV rows, fills, signals and weekly targets of two windows.
"""

import json
import os
import unittest
from pathlib import Path

from backtest.golden import fingerprint
from tests.backtest.test_replay import Replay

GOLDEN = Path(__file__).with_name("golden.json")


class Golden(Replay):
    def test_default_configs_reproduce_the_recorded_run(self):
        got = {}
        for name, (a, b) in {"early": (300, 560), "late": (560, 880)}.items():
            got[name] = fingerprint(self.sim_window(self.days[a], self.days[b]))
        if os.environ.get("GOLDEN_WRITE"):
            GOLDEN.write_text(json.dumps(got, indent=1, sort_keys=True), encoding="utf-8")
        want = json.loads(GOLDEN.read_text(encoding="utf-8"))
        self.assertEqual(got, want)
        self.assertGreater(got["early"]["fillCount"], 10)

    def test_the_fingerprint_does_not_depend_on_the_platform_line_ending(self):
        """pandas ends CSV lines with os.linesep: on Windows the digests would differ from the recorded ones for identical data."""
        from unittest.mock import patch
        r = self.sim_window(self.days[300], self.days[330])
        with patch("os.linesep", "\r\n"):
            windows_like = fingerprint(r)
        self.assertEqual(windows_like, fingerprint(r))

    def sim_window(self, start, end):
        from backtest import replay
        return replay.simulate(self.data, self.targets, self.risk, start, end, 700000.0, keep_signals=True)


if __name__ == "__main__":
    unittest.main()
