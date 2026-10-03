"""Calendar sync: NSE holiday API parsing, index-derived sessions, merge, failure handling."""

import json
import unittest
from datetime import date
from pathlib import Path

import pandas as pd

from app.market import calendar_sync
from tests.app.market_helpers import LOG, Env, weekdays

SYNC = {"homeUrl": "home", "holidayUrl": "api/{year}", "firstYear": 2011, "maxRetries": 1, "backoffSeconds": [0]}


def payload(*entries: tuple[str, str]) -> bytes:
    cm = [{"tradingDate": d, "description": desc, "morning_session": None, "evening_session": None} for d, desc in entries]
    return json.dumps({"CM": cm, "FO": []}).encode()


class FakeClient:
    def __init__(self, years: dict[int, bytes], fail=()):
        self.years, self.fail, self.urls = years, set(fail), []

    def get(self, url: str) -> bytes:
        self.urls.append(url)
        if url == "home":
            if "home" in self.fail:
                raise OSError("down")
            return b""
        year = int(url.split("/")[1])
        if year in self.fail:
            raise OSError("boom")
        return self.years.get(year, json.dumps({"CM": []}).encode())


class ParseTests(unittest.TestCase):
    def test_parse_year_splits_muhurat_from_holidays(self):
        h, s = calendar_sync.parse_year(payload(("26-Jan-2026", "Republic Day"), ("14-Nov-2020", "Diwali-Laxmi Pujan*\r")))
        self.assertEqual((h, s), ({"2026-01-26"}, {"2020-11-14"}))

    def test_index_special_sessions_are_weekend_rows_only(self):
        rows = pd.DataFrame({"Date": ["2025-01-31", "2025-02-01", "2025-02-01", "2025-02-02"]})
        self.assertEqual(calendar_sync.index_special_sessions(rows), {"2025-02-01", "2025-02-02"})

    def test_index_holidays_are_missing_weekdays_before_the_cutoff(self):
        rows = pd.DataFrame({"Date": ["2010-01-04", "2010-01-05", "2010-01-07", "2010-01-08"]})  # 01-06 missing
        self.assertEqual(calendar_sync.index_holidays(rows, date(2010, 1, 8)), {"2010-01-06"})
        self.assertEqual(calendar_sync.index_holidays(rows.iloc[:0], date(2010, 1, 8)), set())

    def test_merge_keeps_known_dates_and_special_wins(self):
        out = calendar_sync.merge({"holidays": ["2020-01-01", "2020-11-14"], "specialSessions": ["2019-10-27"]}, {"2021-01-26"}, {"2020-11-14"})
        self.assertEqual(out, {"holidays": ["2020-01-01", "2021-01-26"], "specialSessions": ["2019-10-27", "2020-11-14"]})


class SyncTests(Env):
    def setUp(self):
        super().setUp()
        self.cfg["calendarSync"] = SYNC
        self.set_indices(["^NSEI"])
        self.cal = Path(self.cfg["paths"]["calendar"])

    def result(self) -> dict:
        return json.loads(self.cal.read_text())

    def test_full_sync_merges_api_and_index_data(self):
        self.set_calendar(holidays=["2026-01-26"], special=["2019-10-27"])
        days = weekdays("2010-01-04", "2010-12-31") +["2011-01-01"]  # 2011-01-01 is a Saturday special
        days.remove("2010-01-06")
        self.yahoo_has("^NSEI", days)
        client = FakeClient({2011: payload(("26-Jan-2011", "Republic Day")), 2020: payload(("14-Nov-2020", "Diwali-Laxmi Pujan*"))})
        calendar_sync.sync(self.cfg, self.fetcher(), LOG, self.report, date(2026, 9, 29), full=True, client=client)
        self.assertEqual(
            self.result(),
            {"holidays": ["2010-01-06", "2011-01-26", "2026-01-26"], "specialSessions": ["2011-01-01", "2019-10-27", "2020-11-14"]},
        )
        self.assertEqual(self.yahoo.calls[0][1], {"period": "max"})
        self.assertEqual(client.urls[1], "api/2011")
        self.assertEqual(client.urls[-1], "api/2027")  # next year's list, once published

    def test_incremental_sync_reads_current_and_next_year_and_the_lookback_window(self):
        self.yahoo_has("^NSEI", ["2026-09-26"])  # a Saturday
        client = FakeClient({2026: payload(("02-Oct-2026", "Mahatma Gandhi Jayanti"))})
        calendar_sync.sync(self.cfg, self.fetcher(), LOG, self.report, date(2026, 9, 29), full=False, client=client)
        self.assertEqual(self.result(), {"holidays": ["2026-10-02"], "specialSessions": ["2026-09-26"]})
        self.assertEqual(client.urls, ["home", "api/2026", "api/2027"])
        self.assertEqual(self.yahoo.calls[0][1], {"start": "2026-08-30", "end": "2026-09-30"})

    def test_unreachable_nse_keeps_file_and_notes_it(self):
        self.set_calendar(holidays=["2026-01-26"])
        self.yahoo_has("^NSEI", ["2026-09-28"])
        calendar_sync.sync(self.cfg, self.fetcher(), LOG, self.report, date(2026, 9, 29), full=False, client=FakeClient({}, fail=["home"]))
        self.assertTrue(any("unreachable" in n for n in self.report.notes))
        self.assertEqual(self.result()["holidays"], ["2026-01-26"])

    def test_one_failed_year_is_skipped_and_noted(self):
        client = FakeClient({2027: payload(("26-Jan-2027", "Republic Day"))}, fail=[2026])
        calendar_sync.sync(self.cfg, self.fetcher(), LOG, self.report, date(2026, 9, 29), full=False, client=client)
        self.assertEqual(self.result()["holidays"], ["2027-01-26"])
        self.assertTrue(any("2026" in n for n in self.report.notes))

    def test_failed_index_fetch_notes_and_derives_nothing(self):
        self.yahoo.script = [OSError("x")] * 5
        client = FakeClient({2026: payload(("02-Oct-2026", "Gandhi Jayanti"))})
        calendar_sync.sync(self.cfg, self.fetcher(), LOG, self.report, date(2026, 9, 29), full=True, client=client)
        self.assertEqual(self.result()["holidays"], ["2026-10-02"])
        self.assertTrue(any("index history unavailable" in n for n in self.report.notes))

    def test_unchanged_calendar_is_not_rewritten_and_missing_config_is_a_no_op(self):
        self.set_calendar(holidays=["2026-10-02"])
        self.yahoo_has("^NSEI", ["2026-09-28"])
        before = self.cal.stat().st_mtime_ns
        client = FakeClient({2026: payload(("02-Oct-2026", "Gandhi Jayanti"))})
        calendar_sync.sync(self.cfg, self.fetcher(), LOG, self.report, date(2026, 9, 29), full=False, client=client)
        self.assertEqual(self.cal.stat().st_mtime_ns, before)
        del self.cfg["calendarSync"]
        calendar_sync.sync(self.cfg, self.fetcher(), LOG, self.report, date(2026, 9, 29), full=True)
