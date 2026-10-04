"""The parameter register guard (Parameter Exposure S1, test 5): no number hides outside the config and the register."""

import re
import unittest
from pathlib import Path

from backtest import register

REPO = Path(__file__).resolve().parents[2]


class Register(unittest.TestCase):
    def test_the_checked_in_register_is_current(self):
        self.assertEqual((REPO / register.REGISTER).read_text(encoding="utf-8"), register.build(REPO),
                         "doc/parameter_register.csv is stale: run `python -m backtest.register --write` and review the diff")

    def test_every_config_leaf_has_a_class(self):
        rows = register.config_rows(REPO)
        self.assertGreater(len(rows), 300)
        self.assertTrue(all(r["class"] in register.CLASSES for r in rows))

    def test_no_unregistered_numeric_literal_in_the_return_affecting_modules(self):
        found = register.literals(REPO)
        missing = [f"{file}:{line} {value!r}" for (file, value), line in found.items() if (file, value) not in register.CODE]
        self.assertEqual(missing, [], "a new hardcoded number: make it a config key (default = today's value) or list it in backtest/register.py CODE")

    def test_the_register_lists_no_literal_that_is_gone(self):
        self.assertEqual(register.unused_code_entries(REPO), [])

    def test_the_scan_catches_a_new_hidden_number(self):
        src = "def f(x):\n    return x * 0.75 + round(x, 3) - 1\n"
        import ast
        tree = ast.parse(src)
        skip = register._skipped(tree)
        values = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, float | int) and id(n) not in skip and n.value not in register.TRIVIAL}
        self.assertEqual(values, {0.75})  # 3 is a rounding digit and 1 is trivial

    def test_the_exposed_keys_are_tunable_or_deliberately_not_and_point_at_a_real_test(self):
        rows = {r["path"]: r for r in register.config_rows(REPO)}
        for key in ("analyst:selector.minMomentum", "analyst:selector.trendBuffer", "analyst:selector.bearScore.confirmThreshold", "analyst:regime.momentumThreshold",
                    "analyst:regime.unknownExtra", "risk:sizing.noTradeBand.floorPct", "risk:ladder.reRisk.rungsPerWeek", "risk:ladder.restartRungOffset"):
            self.assertEqual(rows[key]["class"], "tunable", key)
        for r in rows.values():
            if not r["parity_test"]:
                continue
            path, _, rest = r["parity_test"].partition("::")
            text = (REPO / path).read_text(encoding="utf-8")
            name = rest.split("::")[0].rstrip("*")
            self.assertTrue(re.search(rf"(class|def) {re.escape(name)}", text), f"{r['path']}: {r['parity_test']} not found")

    def test_dead_keys_and_risk_limits_are_not_searchable(self):
        rows = {r["path"]: r for r in register.config_rows(REPO)}
        self.assertEqual(rows["risk:stops.atrMethod"]["class"], "structural")
        self.assertEqual(rows["risk:heat.capPct"]["class"], "risk-limit")
        self.assertEqual(rows["risk:ladder.levels.0.drawdownPct"]["class"], "risk-limit")
        self.assertEqual(rows["backtest:tax.schedule.0.stcgPct"]["class"], "regulatory")


if __name__ == "__main__":
    unittest.main()
