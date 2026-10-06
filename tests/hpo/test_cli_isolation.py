"""Command exit codes, and the isolation rule: hpo imports app and backtest.api only; neither imports hpo; hpo is in no image."""

import ast
import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from hpo import cli
from hpo.errors import Busy, Failed, Refusal
from tests.hpo.fakes import REPO

THIRD_PARTY = {"numpy", "pandas", "scipy", "optuna", "plotly", "yaml", "pyarrow"}


def imports(path: Path):
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            yield from ((a.name, ()) for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            yield node.module, tuple(a.name for a in node.names)


class IsolationTests(unittest.TestCase):
    def test_hpo_imports_only_app_and_backtest_api(self):
        stdlib = __import__("sys").stdlib_module_names
        for path in (p for p in (REPO / "hpo").rglob("*.py") if ".venv" not in p.parts):
            for mod, names in imports(path):
                top = mod.split(".")[0]
                if top in stdlib or top in THIRD_PARTY or top == "hpo" or top == "app":
                    continue
                self.assertEqual(top, "backtest", f"{path}: {mod}")
                self.assertTrue(mod == "backtest.api" or (mod == "backtest" and names == ("api",)), f"{path} imports {mod} {names}: only backtest.api is allowed")

    def test_neither_app_nor_backtest_imports_hpo_and_no_image_copies_it(self):
        for folder in ("app", "backtest"):
            for path in (REPO / folder).rglob("*.py"):
                self.assertFalse(any(m.split(".")[0] == "hpo" for m, _ in imports(path)), str(path))
        for name in ("Dockerfile", "Dockerfile.analyst", "Dockerfile.market", "Dockerfile.risk"):
            self.assertNotRegex((REPO / name).read_text(encoding="utf-8"), r"(?m)^\s*(COPY|ADD)\s+[^\n]*\bhpo\b", name)

    def test_hpo_writes_only_under_its_data_folder_by_construction(self):
        self.assertIn("hpo/data/", (REPO / ".gitignore").read_text(encoding="utf-8"))


class ProbeTests(unittest.TestCase):
    def test_the_probe_report_sets_every_limit_against_what_the_run_reached(self):
        from hpo import settings
        cfg = settings.load("hpo/config/hpo.json")
        out = {"status": "ok", "values": [0.07, 0.12], "constraints": {"min_fills": -100, "fills_per_fold_year": 12.0, "min_exposure": 0.31, "dd_cap": -0.18},
               "metrics": {"cagr": 0.09, "calmar": 0.75, "fills": 400, "avgExposure": 0.09, "foldCagr": [0.1, 0.05], "foldDrawdown": [-0.1, -0.12], "foldFillsPerYear": [30.0, 3.0]},
               "regimeShare": {"BULL": 0.5, "BEAR": 0.5}}
        text = "\n".join(cli.probe_lines(out, cfg, {"risk.sizing.minNewOrderInr": 3000}))
        self.assertIn("ok     fills", text)
        self.assertIn("BREAKS fills per fold-year (worst)", text)
        self.assertIn("BREAKS average exposure", text)
        self.assertIn("9.0%", text)
        self.assertIn("ok     max drawdown depth", text)
        self.assertIn("fold 2:", text)
        self.assertIn("risk.sizing.minNewOrderInr", text)
        days = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2012-01-02", periods=600)]
        nav = pd.Series(np.r_[np.linspace(100, 150, 200), np.linspace(150, 80, 200), np.linspace(80, 120, 200)], index=days)
        out["detail"] = {"nav": nav, "exposure": pd.Series(0.3, index=days), "rung": pd.Series(np.r_[np.zeros(300), np.full(300, 2.0)], index=days),
                         "regimes": {"BULL": {"share": 0.6, "cagr": 0.1, "maxDrawdown": -0.2}, "BEAR": {"share": 0.4, "cagr": None, "maxDrawdown": None}},
                         "stressWindows": {"2008 crisis": {"return": -0.4, "maxDrawdown": -0.46}},
                         "profile": {"fillsPerYear": {"2012": 100, "2013": 80}, "turnover": {"2012": 6.0, "2013": 5.0}, "costDrag": {"2012": 0.03, "2013": 0.025}}}
        full = "\n".join(cli.probe_lines(out, cfg, {}))
        self.assertIn("deepest drawdown 46.7%", full)
        self.assertIn("2008 crisis", full)
        self.assertIn("0: 50%, 2: 50%", full)
        self.assertIn("2012    100", full)
        self.assertIn("status aborted", "\n".join(cli.probe_lines({"status": "aborted", "error": None, "attrs": {"abortReason": "x"}}, cfg, {})))

    def test_probe_rejects_an_unknown_parameter(self):
        self.assertEqual(cli.main(["probe", "--set", "no.such=1"]), 1)

    def test_values_are_parsed_as_numbers_or_booleans(self):
        self.assertEqual([cli._value(x) for x in ("3000", "0.5", "-2", "true", "False")], [3000, 0.5, -2, True, False])


class CliTests(unittest.TestCase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_check_and_space_commands_succeed_without_writing(self):
        self.assertEqual(self.run_cli("--check")[0], 0)
        self.assertEqual(self.run_cli("space", "check")[0], 0)
        code, out, _ = self.run_cli("space", "show", "--stage", "0")
        self.assertEqual(code, 0)
        self.assertIn("risk.sizing.minNewOrderInr", out)

    def test_commands_on_an_unknown_candidate_fail_cleanly(self):
        for cmd in ("promote", "holdout", "gate"):
            self.assertEqual(self.run_cli(cmd, "--candidate", "000000000000")[0], 1, cmd)

    def test_domain_errors_map_to_exit_codes(self):
        for exc, code in ((Busy("x"), 2), (Refusal("x"), 3), (Failed("x"), 1)):
            with patch.object(cli, "cmd_ledger", side_effect=exc):
                self.assertEqual(self.run_cli("ledger", "show")[0], code)

    def test_status_of_an_unknown_study_fails(self):
        self.assertEqual(self.run_cli("study", "status", "--name", "no-such-study")[0], 1)


if __name__ == "__main__":
    unittest.main()
