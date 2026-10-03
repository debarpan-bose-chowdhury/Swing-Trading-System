import io
import json
import logging
import tempfile
import unittest
import unittest.mock
import zipfile
from datetime import date
from pathlib import Path
from unittest.mock import patch

from app.metadata import cleaner, common, data_source, filter as filt, notifier

TODAY = date(2026, 9, 28)
LOG = logging.getLogger("test")
EQUITY_CSV = (
    "SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING\n"
    "AAA,A Ltd,EQ,01-JAN-2000\n"
    "BBB,B Ltd,EQ,01-JAN-2010\n"
    "ONLYLIST,L Ltd,EQ,01-JAN-2010\n"
)
MCAP_CSV = "Symbol,Series,Market Cap(Rs.)\nAAA,EQ,\"3,000,000,000,000\"\nBBB,EQ,600000000000\nONLYCAP,EQ,1\nBAD,EQ,x\n"


def make_zip(mcap_name="MCAP28092026.csv", content=MCAP_CSV) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("PR280926.csv", "ignored")
        zf.writestr(mcap_name, content)
    return buf.getvalue()


def make_cfg(root: Path, buckets=None) -> dict:
    return {
        "filter": {
            "minInceptionDays": 365,
            "capBuckets": buckets
            or [
                {"name": "LargeCap", "minMarketCap": 1000, "topN": 2},
                {"name": "MidCap", "minMarketCap": 100, "topN": 2},
                {"name": "SmallCap", "minMarketCap": 10, "topN": 2},
            ],
        },
        "paths": {"rawData": str(root / "raw"), "storage": str(root / "storage"), "health": str(root / "health.json")},
        "nse": {
            "homeUrl": "http://nse/", "equityListUrl": "http://nse/eq", "dailyReportsUrl": "http://nse/rep",
            "maxRetries": 3, "backoffSeconds": [2, 4, 8],
        },
    }


class TmpCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.cfg = make_cfg(self.root)

    def write_raw(self, rows, day=TODAY):
        common.write_rows(Path(self.cfg["paths"]["rawData"]) / f"{day}.csv", rows)

    def set_health(self, status):
        Path(self.cfg["paths"]["health"]).write_text(json.dumps({"status": status}))

    def storage(self, name, day=TODAY):
        return Path(self.cfg["paths"]["storage"]) / f"{name}_{day}.csv"


class FakeClient:
    def __init__(self, responses):
        self.responses, self.calls = responses, []

    def get(self, url):
        self.calls.append(url)
        r = self.responses[url]
        if isinstance(r, Exception):
            raise r
        return r


class NseClientTests(unittest.TestCase):
    def client(self, retries=3):
        sleeps = []
        nse = {"maxRetries": retries, "backoffSeconds": [2, 4, 8]}
        return data_source.NseClient(nse, sleep=sleeps.append), sleeps

    def test_retries_then_succeeds_with_backoff(self):
        c, sleeps = self.client()
        resp = unittest.mock.MagicMock()
        resp.__enter__.return_value.read.return_value = b"ok"
        with patch.object(c.opener, "open", side_effect=[OSError("x"), OSError("y"), resp]) as op:
            self.assertEqual(c.get("http://x"), b"ok")
        self.assertEqual(op.call_count, 3)
        self.assertEqual(sleeps, [2, 4])

    def test_raises_after_max_retries(self):
        c, sleeps = self.client()
        with patch.object(c.opener, "open", side_effect=OSError("down")) as op:
            with self.assertRaises(OSError):
                c.get("http://x")
        self.assertEqual(op.call_count, 3)
        self.assertEqual(sleeps, [2, 4])

    def test_browser_headers_set(self):
        c, _ = self.client()
        headers = dict(c.opener.addheaders)
        self.assertIn("Mozilla", headers["User-Agent"])
        self.assertIn("Accept", headers)

    def test_zero_retries_raises(self):
        c, _ = self.client(retries=0)
        with self.assertRaises(RuntimeError):
            c.get("http://x")


class ParsingTests(unittest.TestCase):
    def test_listing_handles_bom_spaces_and_missing_fields(self):
        raw = ("﻿" + EQUITY_CSV + "NODATE,N,EQ,\n").encode()
        self.assertEqual(
            data_source.parse_listing_dates(raw),
            {"AAA": "01-JAN-2000", "BBB": "01-JAN-2010", "ONLYLIST": "01-JAN-2010"},
        )

    def reports(self, **entry):
        return {"displayName": "Bhavcopy (PR)(zip)", "filePath": "http://a/b/", "fileActlName": "pr.zip", **entry}

    def test_bhavcopy_url_from_dict_and_list(self):
        for payload in ({"CurrentDay": [{"displayName": "x"}, self.reports()]}, [self.reports()]):
            self.assertEqual(data_source.find_bhavcopy_url(json.dumps(payload).encode()), "http://a/b/pr.zip")

    def test_bhavcopy_missing_or_incomplete(self):
        with self.assertRaises(ValueError):
            data_source.find_bhavcopy_url(b'{"CurrentDay": [{"displayName": "Other"}]}')
        with self.assertRaises(ValueError):
            data_source.find_bhavcopy_url(json.dumps([self.reports(fileActlName="")]).encode())

    def test_market_caps_parse_commas_and_skip_bad(self):
        self.assertEqual(
            data_source.parse_market_caps(make_zip(), LOG),
            {"AAA": 3e12, "BBB": 6e11, "ONLYCAP": 1.0},
        )

    def test_mcap_prefix_case_insensitive_and_in_subdir(self):
        self.assertIn("AAA", data_source.parse_market_caps(make_zip("sub/mcap_x.csv"), LOG))

    def test_zip_without_mcap_file(self):
        with self.assertRaises(ValueError):
            data_source.parse_market_caps(make_zip("other.csv"), LOG)

    def test_mcap_without_usable_rows(self):
        with self.assertRaises(ValueError):
            data_source.parse_market_caps(make_zip(content="Symbol,Other\nA,1\n"), LOG)

    def test_join_is_inner_and_logs_unmatched(self):
        with self.assertLogs(LOG, "WARNING") as cm:
            rows = data_source.join({"A": "d1", "L": "d"}, {"A": 5.0, "C": 1.0}, LOG)
        self.assertEqual(rows, [{"Symbol": "A", "MarketCap": 5.0, "InceptionDate": "d1"}])
        self.assertEqual(len(cm.output), 2)


class DataSourceRunTests(TmpCase):
    def responses(self, **over):
        rep = json.dumps({"CurrentDay": [{"displayName": "Bhavcopy (PR)(zip)", "filePath": "http://nse", "fileActlName": "pr.zip"}]}).encode()
        r = {"http://nse/": b"<html>", "http://nse/eq": EQUITY_CSV.encode(), "http://nse/rep": rep, "http://nse/pr.zip": make_zip()}
        r.update(over)
        return r

    def test_happy_path_writes_raw_and_visits_home_first(self):
        client = FakeClient(self.responses())
        out = data_source.run(self.cfg, TODAY, LOG, client)
        self.assertEqual(client.calls[0], "http://nse/")
        self.assertEqual(out, self.root / "raw" / f"{TODAY}.csv")
        self.assertEqual(
            common.read_rows(out),
            [
                {"Symbol": "AAA", "MarketCap": "3000000000000.0", "InceptionDate": "01-JAN-2000"},
                {"Symbol": "BBB", "MarketCap": "600000000000.0", "InceptionDate": "01-JAN-2010"},
            ],
        )

    def test_fetch_failure_propagates_and_writes_nothing(self):
        client = FakeClient(self.responses(**{"http://nse/eq": OSError("blocked")}))
        with self.assertRaises(OSError):
            data_source.run(self.cfg, TODAY, LOG, client)
        self.assertFalse((self.root / "raw").exists())

    def test_default_client_is_built_from_config(self):
        with patch.object(data_source, "NseClient", return_value=FakeClient(self.responses())) as nc:
            data_source.run(self.cfg, TODAY, LOG)
        nc.assert_called_once_with(self.cfg["nse"])


def raw(symbol, cap, listed="01-JAN-2000"):
    return {"Symbol": symbol, "MarketCap": cap, "InceptionDate": listed}


class FilterTests(TmpCase):
    def run_filter(self, rows):
        self.write_raw(rows)
        filt.run(self.cfg, TODAY, LOG)

    def symbols(self, name):
        return [r["Symbol"] for r in common.read_rows(self.storage(name))]

    def test_buckets_ranges_sorting_and_topn(self):
        self.run_filter([raw("L1", 5000), raw("L2", 3000), raw("L3", 2000), raw("M1", 500), raw("S1", 50), raw("TINY", 5)])
        self.assertEqual(self.symbols("LargeCap"), ["L1", "L2"])  # topN=2; L3 dropped, not demoted
        self.assertEqual(self.symbols("MidCap"), ["M1"])
        self.assertEqual(self.symbols("SmallCap"), ["S1"])  # TINY below every threshold

    def test_threshold_boundaries_inclusive_lower_exclusive_upper(self):
        self.run_filter([raw("A", 1000), raw("B", 999), raw("C", 100), raw("D", 10), raw("E", 9)])
        self.assertEqual(self.symbols("LargeCap"), ["A"])
        self.assertEqual(self.symbols("MidCap"), ["B", "C"])
        self.assertEqual(self.symbols("SmallCap"), ["D"])

    def test_ties_broken_by_symbol_ascending(self):
        self.run_filter([raw("Z", 500), raw("A", 500), raw("M", 500)])
        self.assertEqual(self.symbols("MidCap"), ["A", "M"])

    def test_recent_listings_excluded_exactly_at_365_days(self):
        self.run_filter([raw("OLD", 500, "28-SEP-2025"), raw("NEW", 500, "29-SEP-2025"), raw("ISO", 400, "2020-01-01")])
        self.assertEqual(self.symbols("MidCap"), ["OLD", "ISO"])

    def test_bad_rows_skipped_with_warning(self):
        with self.assertLogs(LOG, "WARNING") as cm:
            self.run_filter([raw("NODATE", 500, "garbage"), raw("NOCAP", "abc"), raw("OK", 500)])
        self.assertEqual(self.symbols("MidCap"), ["OK"])
        self.assertEqual(len(cm.output), 2)

    def test_empty_bucket_still_writes_header_only_file(self):
        self.run_filter([raw("M", 500)])
        self.assertEqual(self.symbols("LargeCap"), [])
        self.assertTrue(self.storage("LargeCap").read_text().startswith("Symbol,MarketCap,InceptionDate"))

    def test_unsorted_config_is_evaluated_highest_threshold_first(self):
        self.cfg["filter"]["capBuckets"].reverse()
        self.run_filter([raw("L", 5000), raw("M", 500)])
        self.assertEqual(self.symbols("LargeCap"), ["L"])
        self.assertEqual(self.symbols("MidCap"), ["M"])

    def test_arbitrary_bucket_count(self):
        self.cfg = make_cfg(self.root, [{"name": "Only", "minMarketCap": 100, "topN": 1}])
        self.run_filter([raw("A", 500), raw("B", 400)])
        self.assertEqual(self.symbols("Only"), ["A"])
        self.assertFalse(self.storage("MidCap").exists())

    def test_missing_raw_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            filt.run(self.cfg, TODAY, LOG)

    def test_unhealthy_flag_warns_but_still_runs(self):
        self.set_health("unhealthy")
        with self.assertLogs(LOG, "WARNING"):
            self.run_filter([raw("M", 500)])
        self.assertEqual(self.symbols("MidCap"), ["M"])

    def test_rerun_same_day_overwrites(self):
        self.run_filter([raw("M", 500)])
        self.run_filter([raw("N", 500)])
        self.assertEqual(self.symbols("MidCap"), ["N"])


class CleanerTests(TmpCase):
    def touch(self, directory, name):
        p = Path(self.cfg["paths"][directory]) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
        return p

    def names(self, directory):
        return sorted(p.name for p in Path(self.cfg["paths"][directory]).iterdir())

    def populate(self):
        for d in ("2026-06-30", "2026-09-28", "2026-08-01"):
            for b in ("LargeCap", "MidCap", "SmallCap"):
                self.touch("storage", f"{b}_{d}.csv")
            self.touch("rawData", f"{d}.csv")

    def test_keeps_only_latest_per_bucket_and_raw(self):
        self.populate()
        cleaner.run(self.cfg, TODAY, LOG)
        self.assertEqual(self.names("storage"), [f"{b}_2026-09-28.csv" for b in ("LargeCap", "MidCap", "SmallCap")])
        self.assertEqual(self.names("rawData"), ["2026-09-28.csv"])

    def test_latest_chosen_by_filename_date_not_mtime(self):
        old, new = self.touch("rawData", "2026-01-01.csv"), self.touch("rawData", "2026-09-28.csv")
        import os
        os.utime(new, (1, 1))
        cleaner.run(self.cfg, TODAY, LOG)
        self.assertEqual(self.names("rawData"), ["2026-09-28.csv"])

    def test_skipped_when_unhealthy(self):
        self.populate()
        self.set_health("unhealthy")
        cleaner.run(self.cfg, TODAY, LOG)
        self.assertEqual(len(self.names("rawData")), 3)
        self.assertEqual(len(self.names("storage")), 9)

    def test_corrupt_health_blocks(self):
        self.populate()
        Path(self.cfg["paths"]["health"]).write_text("{not json")
        cleaner.run(self.cfg, TODAY, LOG)
        self.assertEqual(len(self.names("rawData")), 3)

    def test_missing_health_allows_cleanup(self):
        self.populate()
        cleaner.run(self.cfg, TODAY, LOG)
        self.assertEqual(len(self.names("rawData")), 1)

    def test_unrelated_and_similarly_prefixed_files_untouched(self):
        for n in ("LargeCap_2026-01-01.csv", "LargeCap_2026-09-28.csv", "LargeCapPlus_2026-01-01.csv", "notes.txt", "LargeCap_latest.csv"):
            self.touch("storage", n)
        cleaner.run(self.cfg, TODAY, LOG)
        self.assertEqual(self.names("storage"), sorted(["LargeCap_2026-09-28.csv", "LargeCapPlus_2026-01-01.csv", "LargeCap_latest.csv", "notes.txt"]))

    def test_missing_directories_are_fine(self):
        cleaner.run(self.cfg, TODAY, LOG)

    def test_single_file_never_deleted(self):
        self.touch("rawData", "2026-01-01.csv")
        cleaner.run(self.cfg, TODAY, LOG)
        self.assertEqual(self.names("rawData"), ["2026-01-01.csv"])


class NotifierTests(TmpCase):
    def health(self):
        return json.loads(Path(self.cfg["paths"]["health"]).read_text())

    def make_all(self):
        self.write_raw([])
        for b in self.cfg["filter"]["capBuckets"]:
            common.write_rows(self.storage(b["name"]), [])

    def test_healthy_when_all_present(self):
        self.make_all()
        with self.assertLogs("test", "INFO") as cm:
            result = notifier.run(self.cfg, TODAY, LOG)
        self.assertEqual(result, self.health())
        self.assertEqual(result["status"], "healthy")
        self.assertEqual(result["missing"], [])
        self.assertIn("T", result["checkedAt"])
        self.assertIn("fresh data", cm.output[0])

    def test_unhealthy_lists_missing_bucket_file(self):
        self.make_all()
        self.storage("MidCap").unlink()
        result = notifier.run(self.cfg, TODAY, LOG)
        self.assertEqual(result["status"], "unhealthy")
        self.assertEqual(result["missing"], [str(self.storage("MidCap"))])

    def test_unhealthy_when_raw_missing(self):
        self.make_all()
        (Path(self.cfg["paths"]["rawData"]) / f"{TODAY}.csv").unlink()
        self.assertEqual(len(notifier.run(self.cfg, TODAY, LOG)["missing"]), 1)

    def test_yesterdays_files_do_not_count(self):
        self.write_raw([], date(2026, 9, 27))
        for b in self.cfg["filter"]["capBuckets"]:
            common.write_rows(self.storage(b["name"], date(2026, 9, 27)), [])
        self.assertEqual(len(notifier.run(self.cfg, TODAY, LOG)["missing"]), 4)

    def test_recovers_after_unhealthy(self):
        notifier.run(self.cfg, TODAY, LOG)
        self.assertEqual(self.health()["status"], "unhealthy")
        self.make_all()
        notifier.run(self.cfg, TODAY, LOG)
        self.assertEqual(self.health()["status"], "healthy")


class CommonTests(TmpCase):
    def test_parse_date_formats(self):
        self.assertEqual(common.parse_date(" 05-MAR-2001 "), date(2001, 3, 5))
        self.assertEqual(common.parse_date("2001-03-05"), date(2001, 3, 5))
        self.assertIsNone(common.parse_date(""))
        self.assertIsNone(common.parse_date("nope"))

    def test_load_config_default_and_env(self):
        self.assertEqual(common.load_config()["filter"]["minInceptionDays"], 365)
        p = self.root / "c.json"
        p.write_text('{"a": 1}')
        with patch.dict("os.environ", {"CONFIG_PATH": str(p)}):
            self.assertEqual(common.load_config(), {"a": 1})

    def test_shipped_config_matches_tdd(self):
        cfg = common.load_config()
        self.assertEqual([(b["name"], b["topN"]) for b in cfg["filter"]["capBuckets"]], [("LargeCap", 50), ("MidCap", 50), ("SmallCap", 50)])

    def test_run_stage_logs_to_file_and_exits_nonzero_on_error(self):
        def boom(cfg, today, log):
            raise RuntimeError("boom")
        with patch.object(common, "load_config", return_value=self.cfg), self.assertRaises(SystemExit) as cm, self.assertLogs("s", "ERROR"):
            common.run_stage("s", boom)
        self.assertEqual(cm.exception.code, 1)
        self.assertTrue(any((self.root / "logs").glob("s_*.log")))

    def test_run_stage_success_does_not_exit(self):
        called = []
        with patch.object(common, "load_config", return_value=self.cfg):
            common.run_stage("s", lambda cfg, today, log: called.append(today))
        self.assertEqual(called, [date.today()])


if __name__ == "__main__":
    unittest.main()
