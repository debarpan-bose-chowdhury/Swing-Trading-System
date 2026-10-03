"""NSE surveillance: parsing, normalising, entry/exit rules, stale lists, the stage and the probe (NSE always faked)."""

import argparse
import contextlib
import io
import json
import unittest
from datetime import datetime

from app.market.common import IST
from app.risk import probe, surveil, surveillance
from app.risk.common import Report
from tests.risk_helpers import LOG, Env

SOURCES = {
    "asm": {"url": "https://nse.test/asm.csv", "format": "csv", "symbolColumn": "Symbol", "valueColumn": "Stage", "termColumn": "Term"},
    "gsm": {"url": "https://nse.test/gsm.json", "format": "json", "symbolColumn": "symbol", "valueColumn": "stage", "rowsKey": "data"},
    "t2t": {"url": "https://nse.test/t2t.csv", "format": "csv", "symbolColumn": "SYMBOL", "filterColumn": "SERIES", "filterValues": ["BE", "BZ"]},
    "bands": {"url": "https://nse.test/bands.csv", "format": "csv", "symbolColumn": "Symbol", "valueColumn": "Band"},
}
PAYLOADS = {
    "asm": b"Symbol,Stage,Term\nXYZ,Stage II,LT\nABC,1,ST\n",
    "gsm": json.dumps({"data": [{"symbol": "QRS", "stage": "III"}]}).encode(),
    "t2t": b"SYMBOL,SERIES\nLMN,BE\nOKAY,EQ\nZZZ,BZ\n",
    "bands": b"Symbol,Band\nXYZ,5%\nABC,20\n",
}


class FakeClient:
    def __init__(self, payloads, fail=()):
        self.payloads, self.fail, self.urls = payloads, set(fail), []

    def get(self, url):
        self.urls.append(url)
        name = next((k for k, s in SOURCES.items() if s["url"] == url), None)
        if name in self.fail:
            raise OSError("blocked")
        return b"<html></html>" if name is None else self.payloads[name]


class ParseTests(unittest.TestCase):
    def test_normalise_all_four_sources(self):
        d = surveil.normalise(SOURCES, PAYLOADS, "2026-09-25", "t")
        self.assertEqual(d["sources"], dict.fromkeys(SOURCES, "ok"))
        self.assertEqual(d["asm"], {"LT": {"XYZ": 2}, "ST": {"ABC": 1}})
        self.assertEqual(d["gsm"], {"QRS": 3})
        self.assertEqual(d["t2t"], ["LMN", "ZZZ"])  # EQ series filtered out
        self.assertEqual(d["bandPct"], {"XYZ": 5.0, "ABC": 20.0})

    def test_failed_and_unparseable_sources_are_marked_failed_and_empty(self):
        d = surveil.normalise(SOURCES, {**PAYLOADS, "gsm": None, "bands": b"Symbol\nXYZ\n"}, "2026-09-25", "t")
        self.assertEqual((d["sources"]["gsm"], d["sources"]["bands"], d["sources"]["asm"]), ("failed", "ok", "ok"))
        self.assertEqual(d["gsm"], {})
        d = surveil.normalise(SOURCES, {**PAYLOADS, "asm": b"Wrong,Header\n1,2\n"}, "2026-09-25", "t")
        self.assertEqual((d["sources"]["asm"], d["asm"]), ("ok", {"LT": {}, "ST": {}}))  # no symbol column -> no rows

    def test_stage_parsing(self):
        self.assertEqual([surveil.stage_of(x) for x in ("2", "Stage 3", "Stage II", "IV", "")], [2, 3, 2, 4, 0])

    def test_json_without_rows_key_uses_the_first_list(self):
        rows = surveil.parse_rows(json.dumps({"meta": 1, "rows": [{"a": " 1 "}]}).encode(), "json")
        self.assertEqual(rows, [{"a": "1"}])
        self.assertEqual(surveil.parse_rows(b"[]", "json"), [])


class RuleTests(Env):
    def setUp(self):
        super().setUp()
        self.s = surveil.normalise(SOURCES, {**PAYLOADS, "bands": b"Symbol,Band\nBND,5\nWIDE,20\n"}, self.asof, "t")

    def test_entry_blocks(self):
        for t in ("XYZ", "ABC", "QRS", "LMN", "BND"):  # ASM LT, ASM ST, GSM, T2T, band 5%
            self.assertTrue(surveil.entry_block(self.s, t, self.cfg), t)
        self.assertFalse(surveil.entry_block(self.s, "WIDE", self.cfg))
        self.assertFalse(surveil.entry_block(self.s, "FINE", self.cfg))

    def test_exits_only_on_gsm_or_t2t(self):
        self.assertEqual(surveil.exit_flag(self.s, "QRS", self.cfg), "GSM")
        self.assertEqual(surveil.exit_flag(self.s, "LMN", self.cfg), "T2T")
        for t in ("XYZ", "ABC", "BND", "FINE"):  # ASM and tight bands only warn
            self.assertIsNone(surveil.exit_flag(self.s, t, self.cfg), t)
        self.assertIsNone(surveil.exit_flag(None, "QRS", self.cfg))

    def test_exit_on_follows_config(self):
        self.cfg["surveillance"]["exitOn"] = ["GSM"]
        self.assertIsNone(surveil.exit_flag(self.s, "LMN", self.cfg))

    def test_warnings_for_held_names(self):
        self.assertEqual(surveil.warn_flags(self.s, "XYZ", self.cfg), ["ASM:XYZ"])
        self.assertEqual(surveil.warn_flags(self.s, "BND", self.cfg), ["TIGHT_BAND:BND"])
        self.assertEqual(surveil.warn_flags(None, "XYZ", self.cfg), [])


class LoadTests(Env):
    def test_todays_complete_list_serves_entries_and_exits(self):
        self.surveillance(gsm=["QRS"])
        s = surveil.load(self.cfg, self.context().cal, self.asof)
        self.assertEqual((s["status"], s["entries"], s["asOf"]), ("ok", True, self.asof))
        self.assertIsNotNone(s["exits"])

    def test_missing_list_blocks_entries_and_has_no_exits(self):
        s = surveil.load(self.cfg, self.context().cal, self.asof)
        self.assertEqual((s["status"], s["entries"], s["exits"]), ("missing", False, None))

    def test_a_list_up_to_3_trading_days_old_still_serves_exits_but_not_entries(self):
        self.surveillance(day="2026-09-22")  # Tue; asOf Fri = 3 trading days later
        s = surveil.load(self.cfg, self.context().cal, self.asof)
        self.assertEqual((s["status"], s["entries"]), ("stale", False))
        self.assertIsNotNone(s["exits"])

    def test_a_list_older_than_3_trading_days_is_not_used_for_exits(self):
        self.surveillance(day="2026-09-21")  # Mon: 4 trading days
        self.assertIsNone(surveil.load(self.cfg, self.context().cal, self.asof)["exits"])

    def test_partial_list_blocks_entries_but_serves_exits(self):
        self.surveillance(sources={"asm": "ok", "gsm": "failed", "t2t": "ok", "bands": "ok"})
        s = surveil.load(self.cfg, self.context().cal, self.asof)
        self.assertEqual((s["entries"], s["exits"] is not None), (False, True))

    def test_lists_dated_after_asof_are_ignored(self):
        self.surveillance(day="2026-09-28")
        self.assertEqual(surveil.load(self.cfg, self.context().cal, self.asof)["status"], "missing")


class StageTests(Env):
    def setUp(self):
        super().setUp()
        self.cfg["surveillance"]["sources"] = SOURCES
        self.now = datetime(2026, 9, 25, 20, 15, tzinfo=IST)

    def go(self, client, force=False, now=None):
        report = Report("surveillance")
        surveillance.run(self.cfg, now or self.now, LOG, report, argparse.Namespace(force=force), client=client)
        return report

    def test_downloads_normalises_and_keeps_the_raw_files(self):
        client = FakeClient(PAYLOADS)
        report = self.go(client)
        data = json.loads((self.risk / "surveillance/surveillance_2026-09-25.json").read_text())
        self.assertEqual(data["sources"], dict.fromkeys(SOURCES, "ok"))
        self.assertEqual(report.status(), "ok")
        self.assertEqual(client.urls[0], surveillance.NSE_HOME)  # cookies first
        for name, ext in (("asm", "csv"), ("gsm", "json"), ("t2t", "csv"), ("bands", "csv")):
            self.assertTrue((self.risk / f"surveillance/raw/{name}_2026-09-25.{ext}").exists())
        self.assertTrue(any("Counts" in line for line in report.lines))

    def test_one_failed_source_is_partial_and_is_retried_by_the_next_attempt(self):
        report = self.go(FakeClient(PAYLOADS, fail=["gsm"]))
        self.assertEqual(report.status(), "partial")
        self.assertEqual(json.loads((self.risk / "surveillance/surveillance_2026-09-25.json").read_text())["sources"]["gsm"], "failed")
        again = FakeClient(PAYLOADS)
        self.assertFalse(self.go(again).quiet)  # not complete -> tries again
        self.assertEqual(json.loads((self.risk / "surveillance/surveillance_2026-09-25.json").read_text())["sources"]["gsm"], "ok")

    def test_all_sources_failing_fails_the_run_and_writes_nothing(self):
        report = self.go(FakeClient(PAYLOADS, fail=list(SOURCES)))
        self.assertEqual(report.status(), "failed")
        self.assertFalse((self.risk / "surveillance/surveillance_2026-09-25.json").exists())

    def test_a_complete_list_makes_later_attempts_a_no_op(self):
        self.go(FakeClient(PAYLOADS))
        client = FakeClient(PAYLOADS)
        self.assertTrue(self.go(client).quiet)
        self.assertEqual(client.urls, [])
        self.assertFalse(self.go(FakeClient(PAYLOADS), force=True).quiet)

    def test_unreachable_home_page_does_not_stop_the_downloads(self):
        class NoHome(FakeClient):
            def get(self, url):
                if url == surveillance.NSE_HOME:
                    raise OSError("blocked")
                return super().get(url)

        self.assertEqual(self.go(NoHome(PAYLOADS)).status(), "ok")

    def test_non_trading_day_is_skipped(self):
        client = FakeClient(PAYLOADS)
        self.assertTrue(self.go(client, now=datetime(2026, 9, 26, 20, 15, tzinfo=IST)).quiet)  # Saturday
        self.assertTrue(self.go(client, now=datetime(2026, 10, 2, 20, 15, tzinfo=IST)).quiet)  # holiday
        self.assertEqual(client.urls, [])


class ProbeTests(unittest.TestCase):
    def test_describe_json_csv_and_html(self):
        self.assertIn("columns: symbol, stage", "\n".join(probe.describe("gsm", "u", json.dumps({"data": [{"symbol": "A", "stage": 1}]}).encode())))
        self.assertIn("CSV columns: Symbol, Stage", "\n".join(probe.describe("asm", "u", b"Symbol, Stage\nA,1\n")))
        html = b'<a href="/files/asm_list.csv">x</a><script>load("/api/gsm.json")</script>'
        out = "\n".join(probe.describe("gsm", "u", html))
        self.assertIn("/files/asm_list.csv", out)
        self.assertIn("/api/gsm.json", out)

    def test_probe_prints_findings_and_never_cookies(self):
        class Client:
            def __init__(self, *a, **k):
                pass

            def get(self, url):
                if "gsm" in url.lower():
                    raise OSError("blocked")
                return b"Symbol,Stage\nA,1\n"

        out = io.StringIO()
        from unittest.mock import patch
        with patch.object(probe, "NseClient", Client), contextlib.redirect_stdout(out):
            code = probe.run(["--check-nse", "--url", "https://x.test/extra.csv"])
        text = out.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("asm: CSV columns: Symbol, Stage", text)
        self.assertIn("gsm: failed", text)
        self.assertIn("url1: CSV columns", text)
        self.assertNotIn("cookie", text.lower())

    def test_probe_stops_when_the_home_page_is_unreachable(self):
        class Down:
            def __init__(self, *a, **k):
                pass

            def get(self, url):
                raise OSError("blocked")

        out = io.StringIO()
        from unittest.mock import patch
        with patch.object(probe, "NseClient", Down), contextlib.redirect_stdout(out):
            self.assertEqual(probe.run(["--check-nse"]), 1)
        self.assertIn("not reachable", out.getvalue())

    def test_describe_unparseable_json_falls_back_to_a_csv_header(self):
        self.assertIn("CSV columns", "\n".join(probe.describe("x", "u", b"{not json")))

    def test_probe_check_flag(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(probe.run(["--check-nse", "--check"]), 0)
        self.assertIn("probe: check ok", out.getvalue())


if __name__ == "__main__":
    unittest.main()
