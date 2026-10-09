"""Pinned copies of risk.json, analyst.json and backtest.json as they were before the sD trial-75 configuration (tests/fixtures/config).

The tests assert exact numbers (worked sizing examples, stop clamps, the golden fingerprint), so they read these files, not app/config: retuning or promoting
the live configuration does not break them. `test_the_shipped_*` tests that check the live files only for validity use app/config directly.
"""

import contextlib
import json
import os
import shutil
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
CONFIG = Path(__file__).resolve().parent / "config"


def text(name: str) -> str:
    return (CONFIG / name).read_text(encoding="utf-8")


def load(name: str) -> dict:
    return json.loads(text(name))


@contextlib.contextmanager
def pinned():
    """app.risk / app.analyst load_config() read the pinned files (call with the working directory at the repo root, as at import time)."""
    rel = lambda n: os.path.relpath(CONFIG / n)  # noqa: E731
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch("app.risk.common.CONFIG_PATH", rel("risk.json")))
        stack.enter_context(patch("app.analyst.common.CONFIG_PATH", rel("analyst.json")))
        try:
            from backtest import config as bt_config
        except ImportError:  # the app-only environment has no backtest dependencies
            bt_config = None
        if bt_config is not None:  # config.load() binds its default path at definition time
            stack.enter_context(patch.object(bt_config.load, "__defaults__", (rel("backtest.json"),)))
        yield


def copy_backtest_config(dst: str = "backtest/config") -> None:
    """The repo's backtest/config into dst with backtest.json replaced by the pinned one."""
    shutil.copytree(REPO / "backtest/config", dst, dirs_exist_ok=True)
    shutil.copyfile(CONFIG / "backtest.json", Path(dst) / "backtest.json")


def copy_app_config(dst: str = "app/config") -> None:
    """The repo's app/config into dst (calendar, config.json) with risk.json and analyst.json replaced by the pinned ones."""
    shutil.copytree(REPO / "app/config", dst, dirs_exist_ok=True)
    for n in ("risk.json", "analyst.json"):
        shutil.copyfile(CONFIG / n, Path(dst) / n)
