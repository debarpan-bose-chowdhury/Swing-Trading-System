"""backtest.parity: the comparison logic, on the synthetic world whose live replay the targets tests already match."""

import copy
import json
import unittest
from pathlib import Path

from app.market.tradingcal import Calendar
from backtest import parity
from backtest.targets import Targets
from tests.backtest.test_targets import World


class Helpers(unittest.TestCase):
    def test_sample_dates_takes_the_newest_and_an_even_spread_of_the_rest(self):
        dates = [f"d{i:03d}" for i in range(100)]
        got = parity.sample_dates(dates, 3, 4)
        self.assertEqual([d for d, k in got if k == "recent"], ["d097", "d098", "d099"])
        spread = [d for d, k in got if k == "history"]
        self.assertEqual(len(spread), 4)
        self.assertEqual(spread[0], "d000")
        self.assertLess(spread[-1], "d097")
        self.assertEqual(parity.sample_dates(dates[:2], 5, 5), [("d000", "recent"), ("d001", "recent")])
        self.assertEqual(parity.sample_dates([], 3, 3), [])

    def test_diff_names_what_differs(self):
        a = {"regime": {"raw": "BULL", "active": "BULL"}, "composition": {"LargeCap": 1.0},
             "buckets": {"LargeCap": {"strategy": {"x": 1}, "selected": [{"ticker": "A", "rank": 1, "score": 1.0}, {"ticker": "B", "rank": 2, "score": 0.5}]}}}
        self.assertEqual(parity.diff(a, copy.deepcopy(a), 1e-9), ([], 0.0))
        b = copy.deepcopy(a)
        b["regime"]["active"] = "BEAR"
        b["buckets"]["LargeCap"]["selected"][1] = {"ticker": "C", "rank": 2, "score": 0.5}
        out, _ = parity.diff(a, b, 1e-9)
        self.assertTrue(any("regime" in x for x in out))
        self.assertTrue(any("only backtest ['B'], only live ['C']" in x for x in out))
        c = copy.deepcopy(a)
        c["buckets"]["LargeCap"]["selected"][0]["score"] = 1.01
        self.assertTrue(parity.diff(a, c, 1e-9)[0])
        out, worst = parity.diff(a, c, 0.02, numbers_fail=False)
        self.assertEqual(out, [])
        d = copy.deepcopy(a)
        d["buckets"]["LargeCap"]["selected"].reverse()
        self.assertTrue(any("different order" in x for x in parity.diff(a, d, 1e-9)[0]))


class OnTheSyntheticWorld(World):
    def setUp(self):
        super().setUp()
        self.tg = Targets(self.data, self.cfg)
        self.replay = lambda acfg, d: self.live(d)

    def test_the_backtest_matches_the_apps_replay_on_recent_and_older_dates(self):
        part = parity.check_targets({}, self.cfg, self.tg, 4, 4, self.replay)
        self.assertEqual(len(part["rows"]), 8)
        self.assertEqual({r["status"] for r in part["rows"]}, {"MATCH"}, part["rows"])

    def test_a_different_config_is_caught(self):
        other = copy.deepcopy(self.cfg)
        other["strategies"]["BULL"]["LargeCap"]["top_n"] = 1
        part = parity.check_targets({}, self.cfg, Targets(self.data, other), 6, 0, self.replay)
        self.assertIn("DIFF", {r["status"] for r in part["rows"]})

    def test_a_refused_replay_is_reported_not_counted_as_a_mismatch(self):
        def refuse(acfg, d):
            raise ValueError("bucket file too old")
        part = parity.check_targets({}, self.cfg, self.tg, 2, 0, refuse)
        self.assertEqual({r["status"] for r in part["rows"]}, {"LIVE_REFUSED"})
        self.assertIn("bucket file too old", part["rows"][0]["detail"])

    def test_stored_target_files_are_compared_and_numbers_get_a_tolerance(self):
        d = [x for x, a in zip(self.tg.dates, self.tg.active, strict=True) if a != "Unknown"][-1]
        live = self.live(d)
        folder = Path(self.cfg["paths"]["analyst"]) / "targets"
        folder.mkdir(parents=True)
        pick = next(e["selected"][0] for e in live["buckets"].values() if e["selected"])
        pick["momentum"] = pick["momentum"] * 1.001  # a revised adjusted close: within the stored tolerance
        (folder / f"targets_{d}.json").write_text(json.dumps(live), encoding="utf-8")
        rows = [r for r in parity.check_targets({}, self.cfg, self.tg, 0, 0, self.replay)["rows"] if r["source"] == "stored"]
        self.assertEqual([r["status"] for r in rows], ["MATCH"])
        pick["ticker"] = "ZZZ"  # a different pick is never tolerated
        (folder / f"targets_{d}.json").write_text(json.dumps(live), encoding="utf-8")
        rows = [r for r in parity.check_targets({}, self.cfg, self.tg, 0, 0, self.replay)["rows"] if r["source"] == "stored"]
        self.assertEqual([r["status"] for r in rows], ["DIFF"])

    def test_stored_signals_are_checked_for_regime_rebalance_rule_execution_date_and_caps(self):
        cal = Calendar(self.cfg["paths"]["calendar"])
        from datetime import date as _date
        d = [x for x, a in zip(self.tg.dates, self.tg.active, strict=True) if a != "Unknown" and _date.fromisoformat(x).weekday() == 4][-1]  # a full week
        mine, _ = self.tg.regimes(d)
        from datetime import date

        from app.risk.common import iso
        from app.risk.run import execution_date
        rcfg = {"paths": {"risk": str(self.root / "data/risk")}, "exposure": {"regimeCap": {mine["active"]: 0.8}}}
        sig = {"asOf": d, "regime": dict(mine), "weekly": {"included": True}, "executionDate": iso(execution_date(cal, date.fromisoformat(d))),
               "exposure": {"regimeCap": 0.8, "ladderCap": 1.0, "finalCap": 0.8}}
        folder = self.root / "data/risk/signals"
        folder.mkdir(parents=True)
        (folder / f"signals_{d}.json").write_text(json.dumps(sig), encoding="utf-8")
        rows = parity.check_signals({}, rcfg, self.tg, cal)["rows"]
        self.assertEqual([r["status"] for r in rows], ["MATCH"], rows)
        sig["regime"]["active"] = "BEAR" if mine["active"] != "BEAR" else "BULL"
        sig["executionDate"] = "1999-01-01"
        sig["exposure"]["finalCap"] = 0.9
        (folder / f"signals_{d}.json").write_text(json.dumps(sig), encoding="utf-8")
        row = parity.check_signals({}, rcfg, self.tg, cal)["rows"][0]
        self.assertEqual(row["status"], "DIFF")
        self.assertGreaterEqual(len(row["detail"]), 3)

    def test_summarize_lists_only_the_non_matches(self):
        lines = parity.summarize("targets", {"rows": [{"date": "a", "status": "MATCH", "detail": []}, {"date": "b", "status": "DIFF", "detail": ["x"], "kind": "recent", "source": "replay"}]})
        self.assertIn("2 checked", lines[0])
        self.assertEqual(len(lines), 2)
        self.assertIn("b DIFF", lines[1])


class Cli(World):
    def test_check_lists_the_inputs_and_returns_ok_without_comparing(self):
        import contextlib
        import io
        import shutil

        REPO = Path(__file__).resolve().parents[2]
        shutil.copytree(REPO / "backtest/config", "backtest/config")
        shutil.copytree(REPO / "app/config", "app/config")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = parity.main(["--check"])
        self.assertEqual(code, 0)
        self.assertIn("stored live targets 0, stored live signals 0", out.getvalue())


class Universe(World):
    def test_a_replay_uses_the_newest_bucket_file_unless_the_apps_own_date_rule_is_asked_for(self):
        storage = Path(self.cfg["paths"]["upstreamStorage"])
        for f in list(storage.glob("*_*.csv")):  # every bucket file is now dated after the dates replayed
            b = f.stem.rsplit("_", 1)[0]
            f.rename(storage / f"{b}_2030-01-01.csv")
        tg = Targets(self.data, self.cfg)
        d = [x for x, a in zip(tg.dates, tg.active, strict=True) if a != "Unknown"][-1]
        got = parity.live_replay(self.cfg, d)
        self.assertEqual(parity.diff(tg.build(d), got, 1e-9)[0], [])
        with self.assertRaisesRegex(ValueError, "bucket file on or before"):
            parity.live_replay(self.cfg, d, app_universe=True)

    def test_pick_gaps_and_their_explanation_name_the_cause(self):
        tg = Targets(self.data, self.cfg)
        d = [x for x, a in zip(tg.dates, tg.active, strict=True) if a != "Unknown"][-1]
        mine = tg.build(d)
        live = copy.deepcopy(mine)
        bucket = next(b for b, e in live["buckets"].items() if e["selected"])
        live["buckets"][bucket]["selected"][0]["ticker"] = "GONE"
        gaps = parity.pick_gaps(mine, live)
        self.assertEqual(list(gaps), [bucket])
        only_mine, only_live = gaps[bucket]
        self.assertEqual(only_live, ["GONE"])
        lines = parity.explain_gaps(gaps, tg, self.cfg, d)
        self.assertTrue(any("live-only GONE: in the backtest universe NO" in x for x in lines), lines)
        self.assertTrue(any(f"backtest-only {only_mine[0]}: in the file of the day yes" in x for x in lines), lines)
