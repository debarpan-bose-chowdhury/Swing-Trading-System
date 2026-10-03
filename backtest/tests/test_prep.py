import json
import logging
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from app.market.common import COLS
from app.market.fetcher import EXTRA, Fetcher
from backtest import config, prep
from backtest.tests.helpers import TreeCase, bars, weekdays


def market_cfg() -> dict:
    return {"paths": {"market": "app/data/market"},
            "fetch": {"batchSize": 2, "batchGapSeconds": 0, "hourlyRequestBudget": 2000, "blockPauseMinutes": 60, "maxBlockPausesPerRun": 3,
                      "maxRetries": 0, "backoffSeconds": [1]}}


class PrepTests(TreeCase):
    def setUp(self):
        super().setUp()
        self.days = weekdays("2024-01-01", 40)
        self.bucket("LargeCap", ["AAA", "BBB"])
        f = np.ones(40)
        f[:20] = 0.98
        self.put("AAA", bars("AAA", self.days, 100.0, adj_factor=f))
        self.put("BBB", bars("BBB", self.days, 50.0))
        self.put("^NSEI", bars("^NSEI", self.days, 10000.0), index=True)
        self.cfg = config.load(Path(__file__).parents[1] / "config/backtest.json")
        self.cfg["paths"]["data"] = "backtest/data"
        self.log = logging.getLogger("t")

    def fake_yahoo(self, fail=False):
        def yf(fx, symbols, **kw):
            if fail:
                raise RuntimeError("down")
            rows = []
            for s in symbols:
                d = bars(s, self.days).assign(Dividends=0.0, Splits=0.0)
                if s == "AAA.NS":
                    d.loc[20, "Dividends"] = 2.0
                    d.loc[30, "Splits"] = 2.0
                rows.append(d[COLS + EXTRA])
            return pd.concat(rows, ignore_index=True)
        return patch.object(Fetcher, "_yf", autospec=True, side_effect=yf)

    def test_dividends_and_splits_are_fetched_and_written(self):
        with self.fake_yahoo(), patch("app.market.fetcher.time.sleep"), patch("backtest.prep.market_common.load_config", market_cfg):
            prep.run_dividends(self.cfg, "2024-03-01")
        div = pd.read_csv("backtest/data/dividends.csv")
        self.assertEqual(div.to_dict("records"), [{"Ticker": "AAA", "ExDate": self.days[20], "Amount": 2.0}])
        self.assertEqual(pd.read_csv("backtest/data/splits.csv").Ratio.tolist(), [2.0])

    def test_failed_fetch_writes_nothing(self):
        with self.fake_yahoo(fail=True), patch("app.market.fetcher.time.sleep"), patch("backtest.prep.market_common.load_config", market_cfg):
            with self.assertRaises(RuntimeError):
                prep.run_dividends(self.cfg, "2024-03-01")
        self.assertFalse(Path("backtest/data/dividends.csv").exists())

    def test_scan_writes_report_and_anomalies(self):
        with self.fake_yahoo(), patch("app.market.fetcher.time.sleep"), patch("backtest.prep.market_common.load_config", market_cfg):
            prep.run_dividends(self.cfg, "2024-03-01")
        rep = prep.run_scan(self.cfg)
        self.assertEqual(rep["withHistory"], 2)
        self.assertTrue(rep["dividendsLoaded"])
        self.assertEqual(rep["anomalies"], {"DIV_STEP": 1})
        self.assertEqual(json.loads(Path("backtest/data/data_report.json").read_text())["dataHash"], rep["dataHash"])
        self.assertTrue(Path("backtest/data/anomalies.csv").exists())
        self.assertEqual(prep.run_scan(self.cfg)["dataHash"], rep["dataHash"])  # deterministic

    def test_scan_without_stored_prices_is_exit_3(self):
        import shutil
        shutil.rmtree("app/data/market")
        with patch("backtest.config.load", return_value=self.cfg):
            self.assertEqual(prep.main(["--scan"]), 3)

    def test_check_never_writes(self):
        with patch("backtest.config.load", return_value=self.cfg):
            self.assertEqual(prep.main(["--check"]), 0)
        self.assertFalse(Path("backtest/data").exists())
