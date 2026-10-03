import unittest

import numpy as np
import pandas as pd

from app.market.tradingcal import Calendar
from backtest import checks
from backtest.tests.helpers import bars, weekdays

PREP = {"bigMovePct": 0.25, "dividendStepMin": 0.002, "dividendTolerance": 0.05}


class CalendarTests(unittest.TestCase):
    def cal(self, holidays=(), special=()):
        import json, tempfile, pathlib
        p = pathlib.Path(tempfile.mkdtemp()) / "c.json"
        p.write_text(json.dumps({"holidays": list(holidays), "specialSessions": list(special)}))
        return Calendar(p)

    def test_clean_calendar(self):
        days = weekdays("2020-01-01", 30)
        r = checks.calendar_check(self.cal(holidays=["2020-01-10"]), [d for d in days if d != "2020-01-10"])
        self.assertEqual((r["notTrading"], r["noIndexRow"]), ([], []))

    def test_missing_holiday_and_missing_special_session_and_index_gap(self):
        days = weekdays("2020-01-01", 30)
        idx = [d for d in days if d != "2020-01-10"] + ["2020-01-18"]  # 10th is a holiday; Saturday the 18th traded
        r = checks.calendar_check(self.cal(), idx)
        self.assertEqual(r["notTrading"], ["2020-01-18"])
        self.assertEqual(r["noIndexRow"], ["2020-01-10"])
        self.assertEqual(checks.calendar_check(self.cal(special=["2020-01-18"]), idx)["notTrading"], [])

    def test_years_without_holidays(self):
        r = checks.calendar_check(self.cal(holidays=["2021-01-26"]), weekdays("2020-12-30", 6))
        self.assertEqual(r["yearsWithoutHolidays"], ["2020"])


class ScanTests(unittest.TestCase):
    def frame(self):
        days = weekdays("2024-01-01", 40)
        close = np.full(40, 100.0)
        f = np.ones(40)
        f[:20] = 0.98  # a dividend of 2 on day 20: earlier AdjClose scaled by 1 - 2/100
        return days, bars("AAA", days, close, adj_factor=f)

    def kinds(self, series, divs=None):
        return checks.scan(series, divs, PREP)

    def test_matching_dividend_is_only_a_step(self):
        days, df = self.frame()
        divs = pd.DataFrame({"Ticker": ["AAA"], "ExDate": [days[20]], "Amount": [2.0]})
        out = self.kinds({"AAA": df}, divs)
        self.assertEqual(list(out.Kind), ["DIV_STEP"])
        self.assertAlmostEqual(out.Value.iloc[0], 2.0, places=4)

    def test_unlisted_step_wrong_amount_and_missing_step(self):
        days, df = self.frame()
        self.assertIn("DIV_UNLISTED", set(self.kinds({"AAA": df}).Kind))
        wrong = pd.DataFrame({"Ticker": ["AAA"], "ExDate": [days[20]], "Amount": [3.0]})
        self.assertIn("DIV_AMOUNT", set(self.kinds({"AAA": df}, wrong).Kind))
        none = pd.DataFrame({"Ticker": ["AAA"], "ExDate": [days[5]], "Amount": [1.0]})
        self.assertIn("DIV_NO_STEP", set(self.kinds({"AAA": df}, none).Kind))

    def test_big_move_zero_volume_and_bad_bar(self):
        days = weekdays("2024-01-01", 10)
        close = np.array([100, 100, 140, 140, 140, 140, 140, 140, 140, 140.0])
        df = bars("BBB", days, close)
        df.loc[4, "Volume"] = 0
        df.loc[6, "High"] = 100.0  # high below low
        out = self.kinds({"BBB": df})
        self.assertEqual(set(out.Kind), {"BIG_MOVE", "ZERO_VOLUME", "BAD_BAR"})
        self.assertEqual(out[out.Kind == "BIG_MOVE"].Date.iloc[0], days[2])

    def test_clean_series_has_no_anomalies(self):
        days = weekdays("2024-01-01", 30)
        self.assertTrue(self.kinds({"CCC": bars("CCC", days, np.linspace(100, 110, 30))}).empty)


if __name__ == "__main__":
    unittest.main()
