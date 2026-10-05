"""The parameter space: schema from the register, defaults equal the live configs, every sampled point valid, reparameterisations round-trip."""

import json
import random
import unittest
from unittest.mock import patch

from backtest import api
from hpo import settings as hpo_settings
from hpo import space
from tests.hpo.fakes import REPO


def load_space():
    cfg = hpo_settings.load("hpo/config/hpo.json")
    _, risk, analyst = api.base_configs()
    return cfg, risk, analyst, space.load(cfg, risk, analyst)


def diff(x, y, path="") -> list:
    if isinstance(x, dict):
        return [d for k in x for d in (diff(x[k], y[k], f"{path}{k}.") if k in y else [f"missing {path}{k}"])]
    if isinstance(x, list):
        return [d for i, (a, b) in enumerate(zip(x, y)) for d in diff(a, b, f"{path}{i}.")]
    return [] if x == y else [f"{path}: {x} != {y}"]


class SpaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg, cls.risk, cls.analyst, cls.sp = load_space()

    def test_the_schema_file_is_what_the_register_generates(self):
        doc = space.build_schema(self.cfg["paths"]["register"], self.cfg["paths"]["extraBounds"], self.risk, self.analyst, self.cfg["space"])
        self.assertEqual(doc, json.loads((REPO / "hpo/schema/parameters.schema.json").read_text(encoding="utf-8")), "run `python -m hpo.cli space build`")

    def test_defaults_decode_to_the_live_configs_exactly(self):
        risk, analyst, full = self.sp.decode(self.sp.defaults)
        self.assertEqual(diff(risk, self.risk) + diff(analyst, self.analyst), [])
        self.assertEqual(full, self.sp.complete(self.sp.defaults))

    def test_every_dimension_has_a_valid_default_inside_its_bounds(self):
        for n, d in self.sp.dims.items():
            if d.kind != "bool":
                self.assertLessEqual(d.low, self.sp.defaults[n], n)
                self.assertGreaterEqual(d.high, self.sp.defaults[n], n)

    def test_random_points_always_decode_to_valid_configs(self):
        rng = random.Random(7)
        for _ in range(150):
            pt = {n: (rng.random() < 0.5) if d.kind == "bool" else rng.uniform(d.low, d.high) for n, d in self.sp.dims.items()}
            risk, analyst, _ = self.sp.decode(pt)
            self.assertLess(risk["sizing"]["minAdjustmentInr"], risk["sizing"]["minNewOrderInr"] + 1)
            self.assertLess(analyst["regime"]["smaFast"], analyst["regime"]["smaSlow"])
            self.assertAlmostEqual(sum(analyst["composition"].values()), 1.0, places=9)
            self.assertGreaterEqual(min(analyst["composition"].values()), self.cfg["space"]["compositionFloor"] - 1e-9)
            for lo, hi in risk["stops"]["clampPct"].values():
                self.assertTrue(0 < lo < hi < 1)

    def test_simplex_round_trips(self):
        for floor in (0.0, 0.1):
            w = [0.5, 0.3, 0.2]
            u = space.u_from_simplex(w, floor)
            self.assertEqual([round(x, 9) for x in space.simplex_from_u(u, floor)], w)
            self.assertAlmostEqual(sum(space.simplex_from_u([0.3, 0.9], floor)), 1.0)
            self.assertGreaterEqual(min(space.simplex_from_u([0.0, 0.0], floor)), floor - 1e-12)

    def test_per_bucket_offset_zero_is_the_shared_value_and_an_offset_moves_one_bucket(self):
        base = "analyst.strategies.BULL.lookback"
        d = self.sp.dims[base]
        _, a, _ = self.sp.decode({base: d.low})
        self.assertEqual({a["strategies"]["BULL"][b]["lookback"] for b in self.sp.buckets}, {d.low})
        _, a, _ = self.sp.decode({base: d.low + d.step, f"{base}.off.MidCap": 1})
        self.assertEqual(a["strategies"]["BULL"]["MidCap"]["lookback"], d.low + 2 * d.step)
        self.assertEqual(a["strategies"]["BULL"]["LargeCap"]["lookback"], d.low + d.step)

    def test_minimum_adjustment_never_exceeds_minimum_order_and_the_slow_average_follows_the_fast_one(self):
        risk, analyst, _ = self.sp.decode({"risk.sizing.minNewOrderInr": 2000, "risk.sizing.minAdjRatio": 1.0, "analyst.regime.smaFast": 70, "analyst.regime.smaGap": 80})
        self.assertEqual(risk["sizing"]["minAdjustmentInr"], 2000)
        self.assertEqual(analyst["regime"]["smaSlow"], 150)  # 70 + 80 = 150, the lowest the register allows
        _, analyst, _ = self.sp.decode({"analyst.regime.smaFast": 30, "analyst.regime.smaGap": 80})
        self.assertEqual(analyst["regime"]["smaSlow"], 150)  # 110 clipped up to the register's lowest smaSlow

    def test_a_bound_that_excludes_the_live_value_is_widened_to_it(self):
        self.assertEqual(self.sp.dims["risk.sizing.minNewOrderInr"].high, 25000)
        self.assertIn("risk.sizing.minNewOrderInr", [w["name"] for w in self.sp.widened])

    def test_frozen_classes_stay_off_unless_unfrozen_and_every_report_can_list_them(self):
        names = self.sp.select(["group:ladder"])
        self.assertTrue(names and all(self.sp.dims[n].cls == "tunable" for n in names))
        on = self.sp.select(["group:ladder"], allow_unfreeze=["class:risk-limit"])
        self.assertIn("risk.ladder.levels.0.drawdownPct", on)
        self.assertEqual(set(self.sp.unfrozen(on)), {n for n in on if self.sp.dims[n].cls == "risk-limit"})
        with self.assertRaises(ValueError):
            self.sp.select(["group:nonsense"])

    def test_unknown_names_and_invalid_combinations_are_rejected_before_any_run(self):
        with self.assertRaises(space.InvalidPoint):
            self.sp.decode({"no.such.parameter": 1})
        for validator in ("analyst_common", "risk_common"):  # the app's own validators are the last gate: their ValueError becomes InvalidPoint
            with patch.object(getattr(space, validator), "validate", side_effect=ValueError("rejected")), self.assertRaisesRegex(space.InvalidPoint, "rejected"):
                self.sp.decode(self.sp.defaults)
        self.assertEqual(self.sp.complete({"risk.ladder.levels.1.drawdownGap": -0.5})["risk.ladder.levels.1.drawdownGap"], 0.02)  # repair clips, never penalises

    def test_stage_zero_is_the_sizing_set_that_makes_one_lakh_trade(self):
        zero = self.sp.select(["stage:0"])
        self.assertEqual(set(zero), {"risk.sizing.riskPerPositionPct", "risk.sizing.nameCapPct.LargeCap", "risk.sizing.nameCapPct.MidCap",
                                     "risk.sizing.nameCapPct.SmallCap", "risk.sizing.minNewOrderInr", "risk.sizing.minAdjRatio"})

    def test_windows_facts_cover_the_whole_space(self):
        self.assertGreaterEqual(self.sp.longest_lookback(), 168)
        self.assertGreater(self.sp.warmup_rows(), self.sp.longest_lookback())


if __name__ == "__main__":
    unittest.main()
