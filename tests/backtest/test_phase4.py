"""Dividends, FIFO tax overlay, surveillance proxy, report, and a full single run on a synthetic app tree."""

import json
import shutil
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from app.risk import common as risk_common
from app.risk import surveil
from backtest import (
    config,
    dividends,
    fills,
    pit,
    prep,
    report,
    run,
    surv_proxy,
    tax,
    world,
)
from tests.backtest.helpers import bars, repo_config, weekdays
from tests.backtest.test_targets import World

REPO = Path(__file__).resolve().parents[2]
RISK_CFG = risk_common.load_config("run")
SCHEDULE = json.loads((REPO / "backtest/config/backtest.json").read_text())["tax"]["schedule"]


def fill(date, ticker, side, qty, price, charges, entry=""):
    return {"trade_date": date, "ticker": ticker, "bucket": "LargeCap", "side": side, "qty": qty, "price": price, "charges": charges, "book_entry_date": entry}


class DividendTests(unittest.TestCase):
    def test_credits_held_quantity_on_the_ex_date_only(self):
        b = fills.Book(1000.0)
        b.pos["AAA"] = {"qty": 10, "avg": 100.0, "entry_date": "2024-01-02"}
        d = dividends.Dividends(pd.DataFrame({"Ticker": ["AAA", "BBB"], "ExDate": ["2024-03-01", "2024-03-01"], "Amount": [2.5, 1.0]}))
        self.assertEqual(d.credit(b, "2024-02-29"), [])
        rows = d.credit(b, "2024-03-01")  # BBB is not held
        self.assertEqual([(r["ticker"], r["amountInr"]) for r in rows], [("AAA", 25.0)])
        self.assertEqual(b.cash, 1025.0)

    def test_missing_file_means_no_dividends(self):
        self.assertEqual(dividends.Dividends.load(Path("nope.csv")).by_date, {})


class TaxTests(unittest.TestCase):
    def test_fifo_pieces_and_net_pnl(self):
        f = pd.DataFrame([fill("2024-01-02", "A", "BUY", 10, 100.0, 10.0), fill("2024-06-03", "A", "BUY", 10, 110.0, 10.0),
                          fill("2025-01-10", "A", "SELL", 15, 120.0, 15.0, entry="2024-06-03")])
        p = tax.lots(f)
        self.assertEqual(list(p.qty), [10, 5])
        self.assertEqual([round(x, 6) for x in p.pnl], [180.0, 40.0])  # (20 - 1 - 1) * 10 and (10 - 1 - 1) * 5
        self.assertEqual(list(p.long), [True, False])
        self.assertEqual(list(p.book_long), [False, False])  # the live book dated the whole position from its second buy
        self.assertEqual(tax.entry_view_mismatch(p), {"pieces": 2, "mismatched": 1, "share": 0.5})

    def test_small_long_term_gain_is_inside_the_exemption(self):
        f = pd.DataFrame([fill("2024-01-02", "A", "BUY", 10, 100.0, 0.0), fill("2025-01-10", "A", "SELL", 10, 120.0, 0.0)])
        t = tax.assess(tax.lots(f), SCHEDULE)["2024-25"]
        self.assertEqual((t["netLongTermGainInr"], t["taxInr"]), (200.0, 0.0))

    def test_short_term_rate_by_sale_date_across_the_july_2024_change(self):
        f = pd.DataFrame([fill("2024-05-01", "A", "BUY", 1, 100.0, 0.0), fill("2024-06-03", "A", "SELL", 1, 1100.0, 0.0),
                          fill("2024-08-01", "B", "BUY", 1, 100.0, 0.0), fill("2024-09-02", "B", "SELL", 1, 1100.0, 0.0)])
        t = tax.assess(tax.lots(f), SCHEDULE)["2024-25"]
        self.assertAlmostEqual(t["stcgTaxInr"], 0.15 * 1000 + 0.20 * 1000)
        self.assertAlmostEqual(t["taxInr"], 350 * 1.04)

    def test_long_term_exempt_before_2018_and_taxed_after(self):
        old = pd.DataFrame([fill("2016-01-04", "A", "BUY", 1, 100.0, 0.0), fill("2017-08-01", "A", "SELL", 1, 500000.0, 0.0)])
        self.assertEqual(tax.assess(tax.lots(old), SCHEDULE)["2017-18"]["taxInr"], 0.0)
        new = pd.DataFrame([fill("2019-01-01", "A", "BUY", 1, 100.0, 0.0), fill("2020-06-01", "A", "SELL", 1, 300100.0, 0.0)])
        t = tax.assess(tax.lots(new), SCHEDULE)["2020-21"]
        self.assertAlmostEqual(t["ltcgTaxInr"], 0.10 * (300000 - 100000))

    def test_short_term_loss_offsets_long_term_gain_but_not_the_reverse(self):
        f = pd.DataFrame([fill("2024-01-02", "L", "BUY", 1, 200000.0, 0.0), fill("2025-02-03", "L", "SELL", 1, 400000.0, 0.0),
                          fill("2024-11-01", "S", "BUY", 1, 1000.0, 0.0), fill("2025-01-06", "S", "SELL", 1, 500.0, 0.0)])
        t = tax.assess(tax.lots(f), SCHEDULE)["2024-25"]
        self.assertAlmostEqual(t["netLongTermGainInr"], 199500.0)
        self.assertAlmostEqual(t["ltcgTaxInr"], 0.125 * (199500 - 125000))
        lossy = pd.DataFrame([fill("2024-01-02", "L", "BUY", 1, 200000.0, 0.0), fill("2025-02-03", "L", "SELL", 1, 100000.0, 0.0),
                              fill("2024-11-01", "S", "BUY", 1, 1000.0, 0.0), fill("2025-01-06", "S", "SELL", 1, 1500.0, 0.0)])
        self.assertAlmostEqual(tax.assess(tax.lots(lossy), SCHEDULE)["2024-25"]["stcgTaxInr"], 0.20 * 500)  # a long-term loss shelters nothing

    def test_post_tax_curve_steps_down_on_the_last_day_of_the_financial_year(self):
        days = ["2025-03-27", "2025-03-28", "2025-04-01", "2025-04-02"]
        nav = pd.DataFrame({"date": days, "nav": [100.0] * 4})
        post = tax.post_tax_curve(nav, {"2024-25": {"taxInr": 10.0}})
        self.assertEqual(list(post), [100.0, 90.0, 90.0, 90.0])
        partial = tax.post_tax_curve(nav, {"2025-26": {"taxInr": 5.0}})  # the year is still running: charged on the last simulated day
        self.assertEqual(list(partial), [100.0, 100.0, 100.0, 95.0])


class SurvProxyTests(unittest.TestCase):
    def setUp(self):
        days = weekdays("2024-01-01", 80)
        calm = bars("CALM", days, np.linspace(100, 110, 80))
        close = np.full(80, 100.0)
        locks = [30, 35, 40, 45]  # four limit-like days: a 5% move on a flat range
        for i in locks:
            close[i:] *= 1.05
        locked = bars("LOCK", days, close)
        locked["High"], locked["Low"] = locked.Close, locked.Close * 0.999
        thin = bars("THIN", days, 100.0, volume=10)
        self.days = days
        self.data = pit.PitData({"CALM": calm, "LOCK": locked, "THIN": thin}, calm.assign(Ticker="^NSEI"), {"LargeCap": ["CALM", "LOCK", "THIN"]})
        self.p = {"proxy": True, "circuit": {"movePct": 0.04, "rangePct": 0.01, "window": 20, "minHits": 3}, "thin": {"window": 20, "minValueInr": 5000.0}}

    def test_flags_repeated_circuit_days_and_thin_names(self):
        proxy = surv_proxy.Proxy(self.data, self.p)
        asm, t2t = proxy.flagged(self.days[50])
        self.assertEqual(asm, ["LOCK"])
        self.assertEqual(t2t, ["THIN"])
        self.assertEqual(proxy.flagged(self.days[20])[0], [])  # not enough hits yet

    def test_output_works_with_the_apps_surveillance_rules(self):
        s = surv_proxy.Proxy(self.data, self.p)(self.days[50])
        self.assertTrue(s["entries"])
        self.assertTrue(surveil.entry_block(s["data"], "LOCK", RISK_CFG))
        self.assertFalse(surveil.entry_block(s["data"], "CALM", RISK_CFG))
        self.assertEqual(surveil.exit_flag(s["exits"], "THIN", RISK_CFG), "T2T")
        self.assertIsNone(surveil.exit_flag(s["exits"], "CALM", RISK_CFG))

    def test_off_means_not_modelled(self):
        from backtest.replay import no_surveillance
        self.assertIs(surv_proxy.surveillance_for(self.data, {"surv": {"proxy": False}}), no_surveillance)

    def test_calibration_finds_thresholds_that_reproduce_the_listed_names(self):
        snaps = {d: {"asm": {"LOCK"}, "t2t": {"THIN"}} for d in self.days[46:54]}  # the window still holds three lock days
        grid = {"movePct": [0.04, 0.5], "rangePct": [0.01], "circuitWindow": [20], "minHits": [3], "thinWindow": [20], "minValueInr": [100.0, 5000.0]}
        out = surv_proxy.calibrate(self.data, snaps, grid)
        self.assertEqual((out["asm"].iloc[0].movePct, out["asm"].iloc[0].f1), (0.04, 1.0))
        self.assertEqual((out["t2t"].iloc[0].minValueInr, out["t2t"].iloc[0].f1), (5000.0, 1.0))

    def test_config_refuses_an_enabled_proxy_with_unset_thresholds(self):
        c = json.loads((REPO / "backtest/config/backtest.json").read_text())
        c["surv"]["proxy"] = True
        with self.assertRaises(ValueError):
            config.validate(c)


class SingleRun(World):
    """A full run of `run.single` on a synthetic app tree, with the shipped app configs copied next to it."""

    data_dir = "app/data"

    def setUp(self):
        super().setUp()  # changes into the temp working directory
        shutil.copytree(REPO / "app/config", "app/config", dirs_exist_ok=True)
        Path("app/config/nse_calendar.json").write_text(json.dumps({"holidays": ["2021-01-01"], "specialSessions": []}))  # weekdays only
        self.bt = repo_config()
        self.bt["overrides"]["risk"] = {"sizing": {"minNewOrderInr": 3000, "minAdjustmentInr": 1500}}
        self.bt["tax"]["confirmed"] = True

    def test_single_run_writes_a_labelled_report(self):
        path = run.single(self.bt, self.days[400], self.days[560])
        rep = json.loads(path.read_text())
        self.assertEqual(rep["window"]["start"], self.days[400])
        self.assertGreater(rep["trades"]["fills"], 5)
        self.assertEqual(set(rep["objectives"]), {"postTaxCagr", "maxDrawdown", "ulcerIndex"})
        self.assertTrue(any("survivorship" in x for x in rep["labels"]))
        self.assertTrue(any("NOT modelled" in x for x in rep["labels"]))
        self.assertEqual(len(rep["configHash"]), 16)
        self.assertEqual(rep["dataHash"], self.data.data_hash())
        self.assertIn("2008 crisis", rep["stress"])
        again = json.loads(run.single(self.bt, self.days[400], self.days[560]).read_text())
        for k in ("objectives", "preTax", "postTax", "trades", "tax"):
            self.assertEqual(rep[k], again[k])  # deterministic

    def test_default_start_is_the_first_known_regime_and_one_lakh_does_not_trade_without_overrides(self):
        self.bt["overrides"]["risk"] = {}
        rep = json.loads(run.single(self.bt, None, self.days[300]).read_text())
        self.assertEqual(rep["trades"]["fills"], 0)  # minNewOrderInr 25000 is above every position a Rs 1 lakh account sizes
        self.assertGreaterEqual(rep["window"]["start"], self.days[200])

    def test_compare_is_refused_until_the_adjustment_is_validated(self):
        self.bt["universe"]["adjustValidated"] = False
        with self.assertRaisesRegex(prep.MissingInput, "adjustValidated"):
            run.compare(self.bt, None, None)

    def test_a_set_point_makes_the_hundred_thousand_account_trade_and_the_label_follows_the_mode(self):
        self.bt["overrides"]["risk"] = {}
        Path("backtest/config").mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / "backtest/config/params.json", "backtest/config/params.json")
        pt = world.parse_set(["sizing.minNewOrderInr=3000", "sizing.minAdjustmentInr=1500"])
        self.assertEqual(pt, {"sizing.minNewOrderInr": 3000, "sizing.minAdjustmentInr": 1500})
        rep = json.loads(run.single(self.bt, self.days[400], self.days[560], pt).read_text())
        self.assertGreater(rep["trades"]["fills"], 5)
        self.assertTrue(any("survivorship" in x for x in rep["labels"]))
        self.assertEqual(report.UNIVERSE_LABELS.keys(), {"today", "pit"})
        self.assertNotIn("survivorship", report.UNIVERSE_LABELS["pit"])

    def test_the_report_counts_vanished_names(self):
        rep = json.loads(run.single(self.bt, self.days[400], self.days[560]).read_text())
        self.assertEqual(set(rep["vanished"]), {"exits", "haircut", "writtenOffInr", "examples"})

    def test_composition_mismatch_is_refused(self):
        self.bt["capital"]["composition"] = {"LargeCap": 1.0, "MidCap": 0.0, "SmallCap": 0.0}
        with self.assertRaises(ValueError):
            run.single(self.bt, None, None)


if __name__ == "__main__":
    unittest.main()
