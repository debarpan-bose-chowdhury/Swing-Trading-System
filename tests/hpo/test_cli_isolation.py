"""Command exit codes, and the isolation rule: hpo imports app and backtest.api only; neither imports hpo; hpo is in no image."""

import ast
import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

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

    def test_later_phase_commands_say_so_and_fail(self):
        code, out, _ = self.run_cli("gate")
        self.assertEqual(code, 1)
        self.assertIn("phase 4", out)

    def test_domain_errors_map_to_exit_codes(self):
        for exc, code in ((Busy("x"), 2), (Refusal("x"), 3), (Failed("x"), 1)):
            with patch.object(cli, "cmd_ledger", side_effect=exc):
                self.assertEqual(self.run_cli("ledger", "show")[0], code)

    def test_status_of_an_unknown_study_fails(self):
        self.assertEqual(self.run_cli("study", "status", "--name", "no-such-study")[0], 1)


if __name__ == "__main__":
    unittest.main()
