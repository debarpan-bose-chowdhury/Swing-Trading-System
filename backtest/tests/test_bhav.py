import io
import json
import urllib.error
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

from app.market.tradingcal import Calendar
from backtest import bhav, config, prep
from backtest.tests.helpers import TreeCase, bars, weekdays

CFG = config.load(Path(__file__).resolve().parents[1] / "config/backtest.json")


def legacy_text(day: str, rows: list[tuple], header=None) -> str:
    d = pd.Timestamp(day).strftime("%d-%b-%Y").upper()
    head = header or "SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,TIMESTAMP,TOTALTRADES,ISIN"
    lines = [f"{s},{ser},{c},{c},{c},{c},{c},{c},{v},{c * v},{d},10,INE000A01010" for s, ser, c, v in rows]
    return "\n".join([head, *lines]) + "\n"


def legacy_2007_text(day: str, rows: list[tuple]) -> str:
    """The real early layout: no TOTALTRADES or ISIN, and a trailing comma on every line."""
    d = pd.Timestamp(day).strftime("%d-%b-%Y").upper()
    lines = [f"{s},{ser},{c},{c},{c},{c},{c},{c},{v},{c * v},{d}," for s, ser, c, v in rows]
    return "\n".join(["SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,TIMESTAMP,", *lines]) + "\n"


def udiff_text(day: str, rows: list[tuple]) -> str:
    head = "TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,OpnPric,HghPric,LwPric,ClsPric,LastPric,PrvsClsgPric,TtlTradgVol,TtlTrfVal"
    lines = [f"{day},{day},CM,NSE,STK,1,INE000A01010,{s},{ser},{c},{c},{c},{c},{c},{c},{v},{c * v}" for s, ser, c, v in rows]
    return "\n".join([head, *lines]) + "\n"


def zipped(text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("file.csv", text)
    return buf.getvalue()


class FakeClient:
    """Serves bhavcopy zips from a dict url -> text; an absent url is a 404, a value of None a 500."""

    def __init__(self, cfg, files=None, make=None):
        self.cfg, self.files, self.make, self.calls = cfg, files or {}, make, []

    def get(self, url):
        self.calls.append(url)
        if url == self.cfg["bhav"]["client"]["homeUrl"]:
            return b"<html>"
        day = self.day_of(url)
        text = self.make(day) if self.make else self.files.get(url, False)
        if text is None:
            raise urllib.error.URLError("boom")
        if text is False:
            raise bhav.NotFound(url)
        return zipped(text)

    @staticmethod
    def day_of(url: str) -> str:
        import re
        m = re.search(r"cm(\d{2})([A-Z]{3})(\d{4})bhav", url)
        if m:
            return f"{m.group(3)}-{bhav.MONTHS.index(m.group(2)) + 1:02d}-{m.group(1)}"
        m = re.search(r"_(\d{8})_F_", url)
        return f"{m.group(1)[:4]}-{m.group(1)[4:6]}-{m.group(1)[6:]}"


def many(n=150):
    return [(f"S{i:03d}", "EQ", 100.0 + i, 1000) for i in range(n)]


class ParseTests(TreeCase):
    def setUp(self):
        super().setUp()
        self.cfg = json.loads(json.dumps(CFG))
        self.cfg["paths"]["data"] = "backtest/data"

    def test_url_templates_and_format_switch(self):
        self.assertTrue(bhav.url_for(self.cfg, "2008-01-02").endswith("/2008/JAN/cm02JAN2008bhav.csv.zip"))
        self.assertTrue(bhav.url_for(self.cfg, "2024-07-08").endswith("BhavCopy_NSE_CM_0_0_0_20240708_F_0000.csv.zip"))
        self.assertEqual((bhav.format_for(self.cfg, "2024-07-05"), bhav.format_for(self.cfg, "2024-07-08")), ("legacy", "udiff"))

    def test_both_formats_normalise_and_filter_series(self):
        keep = self.cfg["bhav"]["seriesKeep"]
        a = bhav.parse(legacy_text("2008-01-02", [("AAA", "EQ", 100.5, 10), ("BBB", "N3", 50.0, 5)]), self.cfg["bhav"]["formats"]["legacy"], keep, "2008-01-02")
        self.assertEqual(a.to_dict("records")[0] | {"Isin": ""}, {"Ticker": "AAA", "Date": "2008-01-02", "Series": "EQ", "Open": 100.5, "High": 100.5, "Low": 100.5,
                                                                   "Close": 100.5, "PrevClose": 100.5, "Volume": 10.0, "Value": 1005.0, "Isin": ""})
        self.assertEqual(len(a), 1)  # N3 (a debt series) dropped
        u = bhav.parse(udiff_text("2024-07-09", [("AAA", "EQ", 7.0, 3)]), self.cfg["bhav"]["formats"]["udiff"], keep, "2024-07-09")
        self.assertEqual((u.Ticker[0], u.Close[0], u.Volume[0]), ("AAA", 7.0, 3.0))

    def test_early_files_without_isin_and_with_a_trailing_comma_parse(self):
        got = bhav.parse(legacy_2007_text("2007-09-17", [("AAA", "EQ", 100.5, 10), ("BBB", "BE", 5.0, 1)]), self.cfg["bhav"]["formats"]["legacy"], ["EQ"], "2007-09-17")
        self.assertEqual((len(got), got.Ticker[0], got.Close[0], got.Isin[0]), (1, "AAA", 100.5, ""))

    def test_missing_columns_are_named_and_a_wrong_date_is_refused(self):
        with self.assertRaisesRegex(ValueError, "CLOSE=|TOTTRDQTY"):
            bhav.parse(legacy_text("2008-01-02", [], header="SYMBOL,SERIES,OPEN,HIGH,LOW,LAST,TIMESTAMP,ISIN"), self.cfg["bhav"]["formats"]["legacy"], ["EQ"])
        with self.assertRaises(ValueError):
            bhav.parse(legacy_text("2008-01-03", [("AAA", "EQ", 1.0, 1)]), self.cfg["bhav"]["formats"]["legacy"], ["EQ"], "2008-01-02")


class ProbeDownloadTests(TreeCase):
    def setUp(self):
        super().setUp()
        self.cfg = json.loads(json.dumps(CFG))
        self.cfg["paths"].update(data="backtest/data", appConfig="app/config")
        def make(day):
            if day >= "2024-07-08":
                return udiff_text(day, many())
            return legacy_2007_text(day, many()) if day < "2010" else legacy_text(day, many())  # the real files changed layout over the years
        self.make = make
        self.cal = Calendar("app/config/nse_calendar.json")

    def test_probe_passes_and_unlocks_download(self):
        c = FakeClient(self.cfg, make=self.make)
        res = bhav.probe(self.cfg, c)
        self.assertTrue(res["legacy"]["ok"] and res["udiff"]["ok"])
        self.assertEqual(len(res["legacy"]["days"]), len(self.cfg["bhav"]["probeDays"]["legacy"]))
        self.assertTrue(bhav.probe_passed(self.cfg))
        self.assertTrue((Path("backtest/data/bhav/samples/legacy_2015-06-02.csv")).exists())

    def test_probe_reports_a_bad_mapping_and_keeps_download_locked(self):
        c = FakeClient(self.cfg, make=lambda day: legacy_text(day, many(), header="SYM,SERIES,OPEN,HIGH,LOW,CLOSE,PREVCLOSE,TIMESTAMP,ISIN") if day < "2024-07-08" else udiff_text(day, many()))
        res = bhav.probe(self.cfg, c)
        self.assertFalse(res["legacy"]["ok"])
        self.assertIn("symbol=SYMBOL", res["legacy"]["days"][0]["error"])
        self.assertFalse(bhav.probe_passed(self.cfg))
        with self.assertRaises(prep.MissingInput):
            bhav.download(self.cfg, c, self.cal, "2024-01-01", "2024-01-05", sleep=lambda s: None)

    def test_download_is_resumable_remembers_404s_and_reports_failures(self):
        bhav.probe(self.cfg, FakeClient(self.cfg, make=self.make))
        calls = {}

        def make(day):
            calls[day] = calls.get(day, 0) + 1
            if day == "2024-01-03":
                return False  # NSE says no file
            if day == "2024-01-04":
                return None  # a server error
            return self.make(day)
        c = FakeClient(self.cfg, make=make)
        out = bhav.download(self.cfg, c, self.cal, "2024-01-01", "2024-01-05", sleep=lambda s: None)
        self.assertEqual((out["fetched"], out["missing"], out["failed"]), (3, 1, ["2024-01-04"]))
        raw = Path("backtest/data/bhav/raw")
        self.assertTrue((raw / "2024-01-01.csv").exists() and (raw / "2024-01-03.missing").exists() and not (raw / "2024-01-04.csv").exists())
        again = bhav.download(self.cfg, FakeClient(self.cfg, make=self.make), self.cal, "2024-01-01", "2024-01-05", sleep=lambda s: None)
        self.assertEqual((again["fetched"], again["cached"], again["missing"], again["failed"]), (1, 3, 1, []))

    def test_repeated_failures_stop_the_download_and_keep_what_was_cached(self):
        bhav.probe(self.cfg, FakeClient(self.cfg, make=self.make))
        bad = lambda day: self.make(day) if day < "2024-01-05" else "not,a,bhavcopy\n1,2,3\n"  # noqa: E731
        c = FakeClient(self.cfg, make=bad)
        out = bhav.download(self.cfg, c, self.cal, "2024-01-01", "2024-03-29", sleep=lambda s: None)
        self.assertTrue(out["aborted"])
        self.assertEqual((out["fetched"], len(out["failed"])), (4, 10))
        self.assertEqual(len(c.calls), 1 + 4 + 10)  # home page, the good days, then exactly the allowed failures

    def test_unreadable_file_is_never_cached(self):
        bhav.probe(self.cfg, FakeClient(self.cfg, make=self.make))
        c = FakeClient(self.cfg, make=lambda day: "not,a,bhavcopy\n1,2,3\n")
        out = bhav.download(self.cfg, c, self.cal, "2024-01-01", "2024-01-02", sleep=lambda s: None)
        self.assertEqual(len(out["failed"]), 2)
        self.assertEqual(list(Path("backtest/data/bhav/raw").glob("*.csv")), [])

    def test_build_writes_a_parquet_per_year_and_load_filters_tickers(self):
        bhav.probe(self.cfg, FakeClient(self.cfg, make=self.make))
        bhav.download(self.cfg, FakeClient(self.cfg, make=self.make), self.cal, "2023-12-28", "2024-01-03", sleep=lambda s: None)
        counts = bhav.build(self.cfg)
        self.assertEqual(set(counts), {"2023", "2024"})
        df = bhav.load(self.cfg, {"S001", "S002"})
        self.assertEqual(set(df.Ticker), {"S001", "S002"})
        self.assertEqual(df.Date.min(), "2023-12-28")


class CrosscheckTests(TreeCase):
    def test_spike_split_gaps_and_volume_are_told_apart(self):
        days = weekdays("2024-01-01", 40)
        raw = np.linspace(100, 120, 40)
        b = pd.DataFrame({"Ticker": "AAA", "Date": days, "Series": "EQ", "Open": raw, "High": raw, "Low": raw, "Close": raw, "PrevClose": raw, "Volume": 1000.0, "Value": 1.0, "Isin": ""})
        y = bars("AAA", days, raw.copy(), volume=1000)
        y.loc[5, "Close"] *= 1.05  # one bad bar
        y.loc[20:, "Close"] = y.loc[20:, "Close"]  # raw
        y.loc[:19, "Close"] = y.loc[:19, "Close"] / 2.0  # a 1:2 split on day 20: earlier Yahoo closes are halved
        y.loc[:19, "Volume"] = 2000  # and doubled volume, as Yahoo adjusts both
        y.loc[30, "Volume"] = 5000  # a volume disagreement
        y = y.drop(index=10).reset_index(drop=True)  # Yahoo lacks a day
        b = b.drop(index=15).reset_index(drop=True)  # bhavcopy lacks a day
        out = bhav.crosscheck({"AAA": y}, b, {"closeTolerance": 0.005, "volumeTolerance": 0.10})
        got = {(r.Kind, r.Date) for r in out.itertuples()}
        self.assertIn(("PRICE_SPIKE", days[5]), got)
        self.assertIn(("RATIO_BREAK", days[20]), got)
        self.assertAlmostEqual(out[out.Kind == "RATIO_BREAK"].Value.iloc[0], 2.0, places=2)
        self.assertIn(("NO_YAHOO_ROW", days[10]), got)
        self.assertIn(("NO_BHAV_ROW", days[15]), got)
        self.assertIn(("VOLUME_MISMATCH", days[30]), got)
        self.assertEqual(sum(1 for k, _ in got if k == "PRICE_SPIKE"), 1)  # the return to normal is not a second event
        self.assertEqual(sum(1 for k, _ in got if k == "RATIO_BREAK"), 1)

    def test_identical_series_have_no_findings(self):
        days = weekdays("2024-01-01", 30)
        raw = np.linspace(100, 110, 30)
        b = pd.DataFrame({"Ticker": "AAA", "Date": days, "Series": "EQ", "Open": raw, "High": raw, "Low": raw, "Close": raw, "PrevClose": raw, "Volume": 1000.0, "Value": 1.0, "Isin": ""})
        self.assertTrue(bhav.crosscheck({"AAA": bars("AAA", days, raw.copy(), volume=1000)}, b, {"closeTolerance": 0.005, "volumeTolerance": 0.10}).empty)


class CliTests(TreeCase):
    def test_check_is_offline_and_download_is_locked_without_a_probe(self):
        import shutil
        shutil.copytree(Path(__file__).resolve().parents[1] / "config", "backtest/config")
        self.assertEqual(bhav.main(["--check"]), 0)
        self.assertEqual(bhav.main(["--download"]), 3)
        self.assertFalse(Path("backtest/data").exists())


class DateAndSummaryTests(TreeCase):
    def setUp(self):
        super().setUp()
        self.cfg = json.loads(json.dumps(CFG))
        self.cfg["paths"]["data"] = "backtest/data"

    def test_two_digit_year_dates_are_read(self):
        text = legacy_text("2020-07-13", [("AAA", "EQ", 10.0, 5)]).replace("13-JUL-2020", "13-Jul-20")
        got = bhav.parse(text, self.cfg["bhav"]["formats"]["legacy"], ["EQ"], "2020-07-13")
        self.assertEqual(got.Date[0], "2020-07-13")

    def test_summary_explains_missing_rows_from_the_raw_series_and_classifies_breaks(self):
        raw = Path("backtest/data/bhav/raw")
        raw.mkdir(parents=True)
        (raw / "2020-03-02.csv").write_text(legacy_text("2020-03-02", [("AAA", "BE", 10.0, 5), ("BBB", "EQ", 10.0, 5)]), encoding="utf-8")
        out = pd.DataFrame([
            ("AAA", "2020-03-02", "NO_BHAV_ROW", 0.0, ""), ("CCC", "2020-03-02", "NO_BHAV_ROW", 0.0, ""),
            ("BBB", "2020-04-01", "RATIO_BREAK", 2.0, "yahoo/bhav ratio 1.0000 -> 2.0000"), ("BBB", "2020-05-01", "RATIO_BREAK", 1.37, "yahoo/bhav ratio 1.0000 -> 1.3700"),
            ("BBB", "2020-06-01", "PRICE_SPIKE", 0.2, "yahoo 12.00 vs bhav 10.00"), ("BBB", "2020-06-02", "VOLUME_MISMATCH", 0.5, "report only")],
            columns=["Ticker", "Date", "Kind", "Value", "Detail"])
        text = bhav.summarize(self.cfg, out)
        self.assertIn("'BE': 1", text)  # AAA traded in series BE that day: the EQ-only filter hid it
        self.assertIn("(symbol absent)", text)  # CCC has no row at all in that day's file
        self.assertIn("1 look like split/bonus factors, 1 do not", text)
        self.assertIn("PRICE_SPIKE: 1", text)
        self.assertIn("VOLUME_MISMATCH (report only): 1", text)

    def test_crosscheck_details_carry_the_prices(self):
        days = weekdays("2024-01-01", 20)
        raw = np.linspace(100, 110, 20)
        b = pd.DataFrame({"Ticker": "AAA", "Date": days, "Series": "EQ", "Open": raw, "High": raw, "Low": raw, "Close": raw, "PrevClose": raw, "Volume": 1000.0, "Value": 1.0, "Isin": ""})
        y = bars("AAA", days, raw.copy(), volume=1000)
        y.loc[5, "Close"] *= 1.05
        out = bhav.crosscheck({"AAA": y}, b, {"closeTolerance": 0.005, "volumeTolerance": 0.10})
        self.assertRegex(out[out.Kind == "PRICE_SPIKE"].Detail.iloc[0], r"yahoo \d+\.\d\d vs bhav \d+\.\d\d")


class SeriesAndBasisTests(TreeCase):
    def setUp(self):
        super().setUp()
        self.cfg = json.loads(json.dumps(CFG))
        self.cfg["paths"]["data"] = "backtest/data"

    def test_trade_for_trade_days_are_kept_and_the_first_series_wins_a_duplicate(self):
        text = legacy_text("2020-03-02", [("AAA", "BE", 10.0, 5), ("BBB", "EQ", 20.0, 5), ("BBB", "BE", 99.0, 1), ("CCC", "N3", 5.0, 1)])
        got = bhav.parse(text, self.cfg["bhav"]["formats"]["legacy"], self.cfg["bhav"]["seriesKeep"], "2020-03-02").set_index("Ticker")
        self.assertEqual(sorted(got.index), ["AAA", "BBB"])  # N3 (debt) stays out
        self.assertEqual((got.Series["AAA"], got.Series["BBB"], got.Close["BBB"]), ("BE", "EQ", 20.0))

    def test_adjustment_basis_tells_dividend_adjusted_close_from_price_only(self):
        days = weekdays("2024-01-01", 40)
        data = type("D", (), {})()
        # price-only Close: AdjClose below Close before the dividend on day 20
        f = np.ones(40)
        f[:20] = 0.97
        data.series = {"AAA": bars("AAA", days, 100.0, adj_factor=f)}
        out = pd.DataFrame([("AAA", days[20], "RATIO_BREAK", 1.03, "")], columns=["Ticker", "Date", "Kind", "Value", "Detail"])
        Path("backtest/data").mkdir(parents=True)
        pd.DataFrame({"Ticker": ["AAA"], "ExDate": [days[20]], "Amount": [3.0]}).to_csv("backtest/data/dividends.csv", index=False)
        text = "\n".join(bhav.adjustment_basis(self.cfg, out, data))
        self.assertIn("below 0.99 for 1 of 1", text)
        self.assertIn("coincide with an AdjClose/Close step: 1", text)
        self.assertIn("Yahoo dividends: 1 on the same date, 1 within 3 days", text)


class UniverseStatsTests(TreeCase):
    def test_counts_names_that_stopped_and_the_survivors_among_each_years_top_names(self):
        cfg = json.loads(json.dumps(CFG))
        cfg["paths"]["data"] = "backtest/data"
        out = Path("backtest/data/bhav")
        out.mkdir(parents=True)
        for year, a, b in ((2020, "2020-01-01", 260), (2021, "2021-01-01", 260), (2022, "2022-01-03", 260), (2023, "2023-01-02", 260)):
            days = [d.date().isoformat() for d in pd.bdate_range(a, periods=b) if d.year == year]
            rows = []
            for t, val in (("BIG", 900.0), ("MID", 500.0), ("DEAD", 800.0), ("TINY", 1.0)):
                if t == "DEAD" and year >= 2022:
                    continue  # stops trading at the end of 2021
                rows += [(t, d, val) for d in days]
            pd.DataFrame(rows, columns=["Ticker", "Date", "Value"]).to_parquet(out / f"bhav_{year}.parquet", index=False)
        text = bhav.universe_stats(cfg, {"BIG", "MID"}, top_n=3, window=100, minimum=50)
        self.assertIn("4 symbols ever traded", text)
        self.assertIn("1 do not", text)
        self.assertIn("{'2021': 1}", text)
        self.assertIn("2021: 3 ranked, 2 still trade (67%), 2 are in today's universe; gone e.g. ['DEAD']", text)
        self.assertIn("2022: 3 ranked, 3 still trade (100%)", text)
