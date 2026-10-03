import json
import shutil
from pathlib import Path

import numpy as np
import pandas as pd

from backtest import adjust, bhav, config, links, universe
from backtest.tests.helpers import TreeCase, bars, weekdays

REPO = Path(__file__).resolve().parents[2]


def raw(days, close, prev=None, volume=1000.0):
    c = np.asarray(close, float)
    p = np.r_[c[0], c[:-1]] if prev is None else np.asarray(prev, float)
    return pd.DataFrame({"Date": days, "Open": c, "High": c * 1.01, "Low": c * 0.99, "Close": c, "PrevClose": p, "Volume": volume})


class AdjustTests(TreeCase):
    def test_a_one_for_one_bonus_halves_history_and_doubles_volume(self):
        days = weekdays("2024-01-01", 30)
        close = np.r_[np.full(15, 200.0), np.full(15, 100.0)]
        prev = np.r_[200.0, close[:-1]]
        prev[15] = 100.0  # NSE's reference price on the ex-date is the old close halved
        out, rep = adjust.adjust_security(raw(days, close, prev), "AAA")
        self.assertEqual(list(rep["events"].kind), ["split"])
        self.assertTrue(np.allclose(out.Close, 100.0))
        self.assertTrue((out.Volume[:15] == 2000).all() and (out.Volume[15:] == 1000).all())
        self.assertTrue(np.allclose(out.AdjClose, 100.0))

    def test_a_dividend_moves_adjclose_only(self):
        days = weekdays("2024-01-01", 30)
        close = np.full(30, 100.0)
        prev = np.r_[100.0, close[:-1]]
        prev[20] = 97.0  # a Rs 3 dividend
        out, rep = adjust.adjust_security(raw(days, close, prev), "AAA")
        self.assertEqual(list(rep["events"].kind), ["cash"])
        self.assertTrue(np.allclose(out.Close, 100.0))
        self.assertTrue(np.allclose(out.AdjClose[:20], 97.0) and np.allclose(out.AdjClose[20:], 100.0))
        self.assertTrue((out.Volume == 1000).all())

    def test_reverse_split_and_rounding_noise(self):
        days = weekdays("2024-01-01", 30)
        close = np.r_[np.full(15, 10.0), np.full(15, 50.0)]
        prev = np.r_[10.0, close[:-1]]
        prev[15] = 50.0  # five old shares became one
        prev[5] = 10.02  # rounding noise is ignored
        out, rep = adjust.adjust_security(raw(days, close, prev), "AAA")
        self.assertEqual(list(rep["events"].kind), ["reverse"])
        self.assertTrue(np.allclose(out.Close, 50.0))
        self.assertTrue((out.Volume[:15] == 200).all())

    def test_an_unresolved_break_cuts_the_series_and_keeps_the_part_after_it(self):
        days = weekdays("2024-01-01", 40)
        close = np.r_[np.full(20, 100.0), np.full(20, 55.0)]
        prev = np.r_[100.0, close[:-1]]
        prev[20] = 55.0  # a 45% fall that matches no split ratio (1/0.55 = 1.82): f = 0.55
        out, rep = adjust.adjust_security(raw(days, close, prev), "AAA")
        self.assertEqual(rep["cutAt"], [days[20]])
        self.assertEqual((out.Date.iloc[0], len(out)), (days[20], 20))
        self.assertTrue(np.allclose(out.Close, 55.0))

    def test_a_three_for_five_style_fall_is_read_as_a_bonus_not_a_break(self):
        days = weekdays("2024-01-01", 30)
        close = np.r_[np.full(15, 100.0), np.full(15, 60.0)]
        prev = np.r_[100.0, close[:-1]]
        prev[15] = 60.0
        _, rep = adjust.adjust_security(raw(days, close, prev), "AAA")
        self.assertEqual((list(rep["events"].kind), rep["cutAt"]), (["split"], []))

    def test_output_has_the_stored_layout(self):
        days = weekdays("2024-01-01", 10)
        out, _ = adjust.adjust_security(raw(days, np.linspace(10, 11, 10)), "ZZZ")
        self.assertEqual(list(out.columns), ["Ticker", "Date", "Open", "High", "Low", "Close", "AdjClose", "Volume"])
        self.assertEqual(set(out.Ticker), {"ZZZ"})


class LinkTests(TreeCase):
    def rows(self):
        a = [("OLD", d, "INE1") for d in weekdays("2020-01-01", 20)]
        b = [("NEW", d, "INE1") for d in weekdays("2020-02-03", 20)]
        c = [("DEAD", d, "INE2") for d in weekdays("2020-01-01", 20)]
        early = [("EARLYOLD", d, "") for d in weekdays("2008-01-01", 10)]
        return pd.DataFrame(a + b + c + early, columns=["Ticker", "Date", "Isin"])

    def test_isin_chain_links_a_rename_and_leaves_a_death_alone(self):
        t = links.isin_links(self.rows())
        self.assertEqual(t[["Old", "New", "Source"]].values.tolist(), [["OLD", "NEW", "ISIN"]])
        self.assertGreater(t.GapDays.iloc[0], 0)

    def test_manual_and_nse_sources_and_precedence(self):
        Path("m.csv").write_text("Old,New,Note\nEARLYOLD,LATER,renamed 2008\nOLD,OTHER,manual wins\n")
        manual = links.manual_links(Path("m.csv"))
        Path("n.csv").write_text("Old Sym,New Sym,Eff\nA,B,15-JAN-2010\n")
        nse = links.nse_links(Path("n.csv"), {"old": "Old Sym", "new": "New Sym", "date": "Eff", "dateFormat": "%d-%b-%Y"})
        self.assertEqual(nse.Date[0], "2010-01-15")
        self.assertTrue(links.nse_links(Path("n.csv"), None).empty)  # no mapping, no guessing
        t = links.combine(manual, nse, links.isin_links(self.rows()))
        self.assertEqual(dict(zip(t.Old, t.New))["OLD"], "OTHER")  # the manual row beat the ISIN one
        self.assertEqual(sorted(t.Source), ["MANUAL", "MANUAL", "NSE"])

    def test_chains_resolve_to_the_last_symbol_and_cycles_are_dropped(self):
        t = pd.DataFrame({"Old": ["A", "B", "X", "Y"], "New": ["B", "C", "Y", "X"], "Date": "", "Source": "MANUAL", "GapDays": 0})
        c = links.combine(t)
        self.assertEqual(links.resolve(c)["A"], "C")
        self.assertEqual(sorted(c.Old), ["A", "B"])  # the X <-> Y loop is dropped, never followed forever

    def test_review_list_shows_unlinked_stopped_symbols_by_traded_value(self):
        days = weekdays("2020-01-01", 200)
        rows = pd.DataFrame([(t, d, v) for t, v, n in (("BIGDEAD", 900.0, 100), ("SMALLDEAD", 5.0, 100), ("ALIVE", 500.0, 200), ("RENAMED", 700.0, 100)) for d in days[:n]],
                            columns=["Ticker", "Date", "Value"])
        lk = pd.DataFrame({"Old": ["RENAMED"], "New": ["ALIVE"], "Date": "", "Source": "MANUAL", "GapDays": 0})
        r = links.review_list(rows, lk, days[-1])
        self.assertEqual(list(r.Symbol), ["BIGDEAD", "SMALLDEAD"])


class ValidateTests(TreeCase):
    def setUp(self):
        super().setUp()
        self.cfg = config.load(REPO / "backtest/config/backtest.json")
        self.cfg["paths"]["data"] = "backtest/data"

    def test_derived_series_match_yahoo_and_splits_are_matched(self):
        days = weekdays("2012-01-02", 120)
        close = np.r_[np.full(60, 200.0), np.full(60, 100.0)]
        prev = np.r_[200.0, close[:-1]]
        prev[60] = 100.0
        r = raw(days, close, prev)
        r.insert(0, "Ticker", "AAA")
        y = bars("AAA", days, np.full(120, 100.0), volume=2000)  # Yahoo split-adjusted: 100 throughout
        y.loc[60:, "Volume"] = 1000
        Path("backtest/data").mkdir(parents=True)
        pd.DataFrame({"Ticker": ["AAA"], "ExDate": [days[60]], "Ratio": [2.0]}).to_csv("backtest/data/splits.csv", index=False)
        text = universe.validate_adjust(self.cfg, {"AAA": y}, r, pd.DataFrame(columns=links.LINK_COLS))
        self.assertIn("close within 1% of Yahoo on 100.0%", text)
        self.assertIn("split-like events derived 1, Yahoo splits 1, matched 1; derived but not in Yahoo 0", text)
        self.assertIn("volume median ratio off by >10%: 0", text)


class CliTests(TreeCase):
    def test_links_writes_the_files_and_prints_the_review_names(self):
        shutil.copytree(REPO / "backtest/config", "backtest/config")
        out = Path("backtest/data/bhav")
        out.mkdir(parents=True)
        days = weekdays("2020-01-01", 120)
        rows = [(t, d, "INE9" if t in ("OLD", "NEW") else "", "EQ", 1, 1, 1, 1, 1, 1000.0, v, "") for t, n0, n1, v in
                (("OLD", 0, 50, 5e8), ("NEW", 50, 120, 5e8), ("GONE", 0, 60, 9e8), ("ALIVE", 0, 120, 1e8)) for d in days[n0:n1]]
        df = pd.DataFrame(rows, columns=["Ticker", "Date", "Isin", "Series", "Open", "High", "Low", "Close", "PrevClose", "Volume", "Value", "Extra"]).drop(columns="Extra")
        df.to_parquet(out / "bhav_2020.parquet", index=False)
        text = universe.run_links(config.load())
        self.assertIn("1 symbol links", text)
        self.assertIn("GONE last", text)
        self.assertEqual(pd.read_csv("backtest/data/symbol_links.csv").Old.tolist(), ["OLD"])
        self.assertTrue(Path("backtest/data/symbol_review.csv").exists())
