import json
import shutil
from pathlib import Path

import numpy as np
import pandas as pd

from backtest import adjust, bhav, config, links, universe
from backtest.tests.helpers import TreeCase, bars, weekdays

REPO = Path(__file__).resolve().parents[2]


def raw(days, close, volume=1000.0):
    c = np.asarray(close, float)
    v = np.full(len(c), volume) if np.isscalar(volume) else np.asarray(volume, float)
    return pd.DataFrame({"Date": days, "Open": c, "High": c * 1.01, "Low": c * 0.99, "Close": c, "Volume": v})


def kinds(rep):
    return list(rep["events"].kind)


class AdjustTests(TreeCase):
    def test_a_one_for_one_bonus_halves_history_and_doubles_volume(self):
        days = weekdays("2024-01-01", 40)
        close = np.r_[np.full(20, 200.0), np.full(20, 100.0)]
        vol = np.r_[np.full(20, 1000.0), np.full(20, 2000.0)]
        out, rep = adjust.adjust_security(raw(days, close, vol), "AAA")
        self.assertEqual(kinds(rep), ["split"])
        self.assertTrue(np.allclose(out.Close, 100.0) and np.allclose(out.AdjClose, 100.0))
        self.assertTrue((out.Volume == 2000).all())

    def test_a_reverse_split_is_recognised_by_the_volume_falling_the_same_way(self):
        days = weekdays("2024-01-01", 40)
        close = np.r_[np.full(20, 10.0), np.full(20, 50.0)]
        vol = np.r_[np.full(20, 1000.0), np.full(20, 200.0)]
        out, rep = adjust.adjust_security(raw(days, close, vol), "AAA")
        self.assertEqual(kinds(rep), ["reverse"])
        self.assertTrue(np.allclose(out.Close, 50.0) and (out.Volume == 200).all())

    def test_a_crash_whose_volume_spike_fades_is_a_real_move_and_is_kept(self):
        days = weekdays("2024-01-01", 40)
        close = np.r_[np.full(20, 100.0), np.full(20, 55.0)]
        vol = np.full(40, 1000.0)
        vol[20:22] = 20000.0  # the news day and the next
        out, rep = adjust.adjust_security(raw(days, close, vol), "AAA")
        self.assertEqual((kinds(rep), rep["cutAt"], len(out)), (["move"], [], 40))
        self.assertEqual((out.Close.iloc[0], out.Close.iloc[-1]), (100.0, 55.0))  # untouched: the loss is real

    def test_an_unexplained_break_cuts_the_series_and_keeps_the_part_after_it(self):
        days = weekdays("2024-01-01", 40)
        close = np.r_[np.full(20, 100.0), np.full(20, 55.0)]  # 1.82x is no usual factor
        vol = np.r_[np.full(20, 1000.0), np.full(20, 3000.0)]  # and the volume level shifted for good
        out, rep = adjust.adjust_security(raw(days, close, vol), "AAA")
        self.assertEqual(rep["cutAt"], [days[20]])
        self.assertEqual((out.Date.iloc[0], len(out)), (days[20], 20))

    def test_small_moves_and_the_end_of_the_data_are_left_alone(self):
        days = weekdays("2024-01-01", 30)
        close = np.r_[np.full(15, 100.0), np.full(15, 75.0)]  # -25%: an ordinary (large) day, below the 30% threshold
        _, rep = adjust.adjust_security(raw(days, close), "AAA")
        self.assertEqual(kinds(rep), [])
        close2 = np.r_[np.full(27, 100.0), np.full(3, 50.0)]  # a halving three sessions before the data ends: too few sessions to judge
        out, rep2 = adjust.adjust_security(raw(weekdays("2024-01-01", 30), close2), "AAA")
        self.assertEqual((kinds(rep2), rep2["cutAt"], len(out)), (["pending"], [], 30))

    def test_price_noise_around_the_factor_still_matches(self):
        days = weekdays("2024-01-01", 40)
        close = np.r_[np.full(20, 200.0), np.full(20, 96.0)]  # 2.08x: a 2:1 plus a 4% market move
        vol = np.r_[np.full(20, 1000.0), np.full(20, 1800.0)]
        _, rep = adjust.adjust_security(raw(days, close, vol), "AAA")
        self.assertEqual(kinds(rep), ["split"])

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
        Path("n.csv").write_text('3M India Limited,BIRLA3M,3MINDIA,15-JUN-2004\nAcme, Holdings and Sons Ltd,ACMEOLD,ACMENEW,02-FEB-2010\nbroken line\n')
        layout = {"fromEnd": {"old": 3, "new": 2, "date": 1}, "dateFormat": "%d-%b-%Y"}
        nse = links.nse_links(Path("n.csv"), layout)
        self.assertEqual(nse[["Old", "New", "Date"]].values.tolist(), [["BIRLA3M", "3MINDIA", "2004-06-15"], ["ACMEOLD", "ACMENEW", "2010-02-02"]])  # headerless, commas in a name, bad lines skipped
        self.assertTrue(links.nse_links(Path("n.csv"), None).empty)  # no layout, no guessing
        t = links.combine(manual, nse, links.isin_links(self.rows()))
        self.assertEqual(dict(zip(t.Old, t.New))["OLD"], "OTHER")  # the manual row beat the ISIN one
        self.assertEqual(set(t.Source), {"MANUAL", "NSE"})  # the ISIN row for OLD lost to the manual row

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
        rows = pd.concat([rows, pd.DataFrame([("IBUL-RE", d, 800.0) for d in days[:100]], columns=["Ticker", "Date", "Value"])])
        self.assertIn("IBUL-RE", set(links.review_list(rows, lk, days[-1]).Symbol))
        self.assertNotIn("IBUL-RE", set(links.review_list(rows, lk, days[-1], exclude=r"-RE\d*$").Symbol))  # a rights entitlement is no company


class ValidateTests(TreeCase):
    def setUp(self):
        super().setUp()
        self.cfg = config.load(REPO / "backtest/config/backtest.json")
        self.cfg["paths"]["data"] = "backtest/data"

    def test_derived_series_match_yahoo_and_splits_are_matched(self):
        days = weekdays("2012-01-02", 120)
        close = np.r_[np.full(60, 200.0), np.full(60, 100.0)]
        r = raw(days, close, np.r_[np.full(60, 1000.0), np.full(60, 2000.0)])
        r.insert(0, "Ticker", "AAA")
        y = bars("AAA", days, np.full(120, 100.0), volume=2000)  # Yahoo split-adjusted: 100 throughout
        # Yahoo's split-adjusted volume is on the new share basis throughout: 2000 before and after
        Path("backtest/data").mkdir(parents=True)
        pd.DataFrame({"Ticker": ["AAA"], "ExDate": [days[60]], "Ratio": [2.0]}).to_csv("backtest/data/splits.csv", index=False)
        text = universe.validate_adjust(self.cfg, {"AAA": y}, r, pd.DataFrame(columns=links.LINK_COLS))
        self.assertIn("close within 1% of Yahoo on 100.0%", text)
        self.assertIn("split-like events derived 1, Yahoo splits 1, matched 1; derived but not in Yahoo 0", text)
        self.assertIn("volume median ratio off by >10%: 0", text)
        self.assertIn("unresolved cuts 0", text)


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
