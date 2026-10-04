import copy
import json
from pathlib import Path

import numpy as np
import pandas as pd

from backtest import pit, pituniverse as pu, prep
from tests.backtest.helpers import repo_config, TreeCase, bars, weekdays

REPO = Path(__file__).resolve().parents[2]
SIZES = {"LargeCap": 2, "MidCap": 2, "SmallCap": 2}


def panel(days, values: dict) -> pd.DataFrame:
    return pd.DataFrame({k: pd.Series(v, index=days, dtype="float32") for k, v in values.items()}).sort_index()


class RankingTests(TreeCase):
    def setUp(self):
        super().setUp()
        self.days = weekdays("2020-01-01", 300)

    def test_trailing_median_needs_enough_observations_and_uses_only_the_past(self):
        v = np.arange(300, dtype=float)
        v[200:] = 1e9  # a later spike must not leak into an earlier date
        p = panel(self.days, {"A": v, "B": np.where(np.arange(300) < 150, np.nan, 5.0)})
        got = pu.trailing_median(p, [self.days[99], self.days[199], self.days[160]], window=50, min_obs=30)
        self.assertAlmostEqual(float(got.A.iloc[0]), float(np.median(np.arange(50, 100))))
        self.assertAlmostEqual(float(got.A.iloc[1]), float(np.median(np.arange(150, 200))))  # the spike after day 199 is not seen
        self.assertTrue(np.isnan(got.B.iloc[0]))  # B has no data yet
        self.assertTrue(np.isnan(got.B.iloc[2]))  # 10 observations < 30

    def test_scope_is_everyone_who_was_ever_near_the_top_dead_names_included_and_rights_excluded(self):
        d = self.days
        p = panel(d, {"OLDBIG": np.r_[np.full(120, 900.0), np.full(180, np.nan)], "NEWBIG": np.r_[np.full(120, 100.0), np.full(180, 800.0)],
                      "MID": np.full(300, 300.0), "TINY": np.full(300, 1.0), "IBUL-RE": np.full(300, 5000.0)})
        scope = pu.scope_symbols(p, top=2, window=40, min_obs=20, exclude=r"-RE\d*$")
        self.assertEqual(scope, ["MID", "NEWBIG", "OLDBIG"])  # TINY was never top 2; the entitlement is no company

    def test_bands_are_filled_in_rank_order_and_unusable_names_are_skipped_and_counted(self):
        days = self.days
        vals = {f"S{i}": np.full(300, 1000.0 - 10 * i) for i in range(8)}
        p = panel(days, vals)
        med = pu.trailing_median(p, [days[250]], 40, 20)
        mem, holes = pu.memberships(med, lambda s, d: s != "S1", SIZES, None)  # S1 has no usable series
        got = {b: list(g.sort_values("Rank").Symbol) for b, g in mem.groupby("Bucket")}
        self.assertEqual(got, {"LargeCap": ["S0", "S2"], "MidCap": ["S3", "S4"], "SmallCap": ["S5", "S6"]})
        self.assertEqual(holes[days[250]], 1)  # S1 would have been in the top 6

    def test_membership_on_a_day_does_not_depend_on_later_values(self):
        days = self.days
        base = {f"S{i}": np.full(300, 1000.0 - 10 * i) for i in range(6)}
        later = {k: v.copy() for k, v in base.items()}
        later["S5"][260:] = 1e9
        a = pu.memberships(pu.trailing_median(panel(days, base), [days[250]], 40, 20), lambda s, d: True, SIZES, None)[0]
        b = pu.memberships(pu.trailing_median(panel(days, later), [days[250]], 40, 20), lambda s, d: True, SIZES, None)[0]
        pd.testing.assert_frame_equal(a, b)

    def test_renamed_symbols_are_one_name_in_the_value_panel(self):
        rows = pd.DataFrame([("OLD", d, 10.0) for d in self.days[:50]] + [("NEW", d, 20.0) for d in self.days[50:100]] + [("NEW", self.days[10], 99.0)], columns=["Ticker", "Date", "Value"])
        p = pu.value_panel(rows, {"OLD": "NEW"})
        self.assertEqual(list(p.columns), ["NEW"])
        self.assertEqual((float(p.NEW.iloc[0]), float(p.NEW.iloc[60])), (10.0, 20.0))
        self.assertEqual(float(p.NEW.loc[self.days[10]]), 99.0)  # the larger value wins an overlap day

    def test_membership_lookup_uses_the_newest_date_on_or_before(self):
        tbl = pd.DataFrame([("2020-01-10", "LargeCap", "A", 1), ("2020-01-17", "LargeCap", "B", 1)], columns=["Date", "Bucket", "Symbol", "Rank"])
        m = pu.Membership(tbl, ["LargeCap", "MidCap"])
        self.assertEqual((m.at("2020-01-09")["LargeCap"], m.at("2020-01-12")["LargeCap"], m.at("2020-02-01")["LargeCap"]), (set(), {"A"}, {"B"}))
        self.assertEqual(m.at("2020-01-12")["MidCap"], set())

    def test_usable_means_a_recent_price_row(self):
        s = bars("X", self.days[:100], 10.0)
        self.assertTrue(pu.usable(s, self.days[99]) and pu.usable(s, self.days[101]))  # two sessions later: a halt, not a death
        self.assertFalse(pu.usable(s, self.days[150]))


class BuildTests(TreeCase):
    """End to end on synthetic bhavcopy files: build, attach, and the loader refuses pit mode until the adjustment is validated."""

    def setUp(self):
        super().setUp()
        Path("app/config/config.json").write_text(json.dumps({"filter": {"capBuckets": [{"name": b, "topN": n} for b, n in SIZES.items()]}}))
        self.cfg = repo_config()
        self.cfg["paths"].update(data="backtest/data", appConfig="app/config", appData="app/data")
        self.days = weekdays("2020-01-01", 260)
        names = {"BIG": 900.0, "DEAD": 800.0, "MID": 500.0, "REN": 400.0, "SMALL": 300.0, "TINY": 1.0, "IBUL-RE": 5000.0}
        rows = []
        for t, val in names.items():
            for i, d in enumerate(self.days):
                if t == "DEAD" and i >= 150:
                    continue
                rows.append((t, d, "EQ", 100.0, 101.0, 99.0, 100.0, 100.0, 1000.0, val * 1e6 * (1 + (i % 3) * 0.01), ""))
        df = pd.DataFrame(rows, columns=["Ticker", "Date", "Series", "Open", "High", "Low", "Close", "PrevClose", "Volume", "Value", "Isin"])
        out = Path("backtest/data/bhav")
        out.mkdir(parents=True)
        df.to_parquet(out / "bhav_2020.parquet", index=False)
        self.yahoo = {"BIG": bars("BIG", self.days, 100.0, volume=1000), "MID": bars("MID", self.days, 100.0, volume=1000)}
        self.index_dates = self.days

    def test_build_prices_dead_names_from_the_bhavcopy_and_labels_by_liquidity(self):
        r = pu.build(self.cfg, self.yahoo, self.index_dates)
        self.assertEqual(r["fromYahoo"], 2)
        self.assertEqual(r["derived"], 4)  # DEAD, REN, SMALL, TINY... scope decides which of them (TINY is top-6 here)
        mem = pd.read_parquet("backtest/data/pit/membership.parquet")
        last = mem[mem.Date == mem.Date.max()]
        self.assertEqual(set(last[last.Bucket == "LargeCap"].Symbol), {"BIG", "MID"})  # DEAD has stopped by the end
        self.assertNotIn("IBUL-RE", set(mem.Symbol))
        early = mem[(mem.Date == mem.Date[mem.Date <= self.days[100]].max())]
        self.assertEqual(set(early[early.Bucket == "LargeCap"].Symbol), {"BIG", "DEAD"})  # alive and second-biggest then
        self.assertEqual(r["scopeWithoutUsableSeries"], 0)

    def test_attach_switches_the_data_to_point_in_time_labels(self):
        pu.build(self.cfg, self.yahoo, self.index_dates)
        data = pit.PitData(copy.deepcopy(self.yahoo), self.yahoo["BIG"].assign(Ticker="^NSEI"), {"LargeCap": ["BIG"], "MidCap": [], "SmallCap": []})
        h0 = data.data_hash()
        self.assertEqual(data.members("2020-06-01")["LargeCap"], {"BIG"})  # static before
        pu.attach(data, self.cfg)
        self.assertIn("DEAD", data.series)
        self.assertEqual(data.members(self.days[100])["LargeCap"], {"BIG", "DEAD"})
        self.assertEqual(data.members("2019-01-01")["LargeCap"], set())
        self.assertNotEqual(h0, data.data_hash())

    def test_pit_mode_is_refused_until_the_adjustment_is_validated(self):
        shutil_cfg = copy.deepcopy(self.cfg)
        shutil_cfg["universe"]["mode"] = "pit"
        shutil_cfg["universe"]["adjustValidated"] = False
        for t, d in (("BIG", self.yahoo["BIG"]),):
            self.eq.rebuild(t, d)
        self.idx.rebuild("NSEI", self.yahoo["BIG"].assign(Ticker="^NSEI"))
        self.bucket("LargeCap", ["BIG"])
        with self.assertRaisesRegex(prep.MissingInput, "adjustValidated"):
            prep.load_pit(shutil_cfg)
        shutil_cfg["universe"]["adjustValidated"] = True
        with self.assertRaisesRegex(prep.MissingInput, "build-pit"):
            prep.load_pit(shutil_cfg)  # validated, but the layer has not been built


from backtest import replay  # noqa: E402
from backtest.targets import Targets  # noqa: E402
from tests.backtest.test_replay import Replay  # noqa: E402
from tests.backtest.test_targets import BUCKETS  # noqa: E402


class PitModeEngine(Replay):
    """The engine in point-in-time mode, on the synthetic world: with a membership that equals the static buckets it must reproduce
    today-mode results exactly; with a different membership only members can be picked."""

    def membership(self, drop=(), move=None) -> pu.Membership:
        dates = Targets(self.data, self.cfg).dates
        rows = []
        for d in dates:
            for b, syms in BUCKETS.items():
                for i, s in enumerate(syms):
                    if s in drop:
                        continue
                    bucket = move[1] if move and s == move[0] and d >= move[2] else b
                    rows.append((d, bucket, s, i + 1))
        return pu.Membership(pd.DataFrame(rows, columns=["Date", "Bucket", "Symbol", "Rank"]), list(BUCKETS))

    def pit_data(self, mem) -> pit.PitData:
        d = pit.PitData(copy.deepcopy(self.data.series), self.data.index.copy(), self.data.buckets)
        d.add_pit({}, mem)
        return d

    def test_static_equivalent_membership_reproduces_the_static_engine(self):
        d = self.pit_data(self.membership())
        a = Targets(self.data, self.cfg)
        b = Targets(d, self.cfg)
        for day in [x for x in a.dates if x >= self.days[300]][::7]:
            self.assertEqual(a.build(day)["buckets"], b.build(day)["buckets"], day)
        start, end = self.days[400], self.days[470]
        s1 = replay.simulate(self.data, a, self.risk, start, end, 700000.0)
        s2 = replay.simulate(d, b, self.risk, start, end, 700000.0)
        pd.testing.assert_frame_equal(s1.nav, s2.nav)
        pd.testing.assert_frame_equal(s1.fills, s2.fills)
        self.assertGreater(len(s1.fills), 0)

    def test_only_members_can_be_picked_and_labels_can_change_over_time(self):
        picks_all = {p["ticker"] for day in Targets(self.data, self.cfg).dates[100:140] for e in Targets(self.data, self.cfg).build(day)["buckets"].values() for p in e["selected"]}
        drop = sorted(picks_all)[:2]
        mem = self.membership(drop=drop)
        tg = Targets(self.pit_data(mem), self.cfg)
        got = {p["ticker"] for day in tg.dates[100:140] for e in tg.build(day)["buckets"].values() for p in e["selected"]}
        self.assertTrue(got and not (got & set(drop)))
        later = self.membership(move=("S1", "LargeCap", self.days[500]))
        self.assertIn("S1", later.at(self.days[600])["LargeCap"])
        self.assertNotIn("S1", later.at(self.days[400])["LargeCap"])
