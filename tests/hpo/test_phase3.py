"""Phase 3: front selection, TPE (and GP when PyTorch is installed) honouring constraints on a landscape with a known optimum, blocks and chaining, stage advice."""

import importlib.util
import io
import tempfile
import unittest
from pathlib import Path

from hpo import pareto, stages
from hpo.errors import Failed
from hpo.study import Study, read_records
from tests.hpo.fakes import FakeRunner, make_settings
from tests.hpo.test_space import load_space

ACTIVE = ["risk.stops.atrMultiplier", "risk.sizing.riskPerPositionPct", "risk.sizing.minNewOrderInr", "risk.sizing.nameCapPct.MidCap", "risk.sizing.noTradeBand.relative",
          "risk.liquidity.advDays"]
OPTIMUM = 0.25  # landscape() peaks here


class SelectTests(unittest.TestCase):
    ROWS = [{"trial": 0, "f1": 0.05, "f2": 0.05, "calmar": 1.0}, {"trial": 1, "f1": 0.15, "f2": 0.08, "calmar": 1.9}, {"trial": 2, "f1": 0.20, "f2": 0.20, "calmar": 1.0},
            {"trial": 3, "f1": 0.22, "f2": 0.28, "calmar": 0.8}]

    def test_calmar_picks_the_best_ratio_and_the_knee_the_bulge_of_the_front(self):
        self.assertEqual(pareto.select(self.ROWS, "calmar")["trial"], 1)
        self.assertEqual(pareto.select(self.ROWS, "knee")["trial"], 1)  # 0.15 CAGR for 8% depth sits far below the chord from (0.05, 0.05) to (0.22, 0.28)

    def test_missing_calmar_and_tiny_fronts_and_errors(self):
        self.assertEqual(pareto.select([{"trial": 7, "f1": 0.1, "f2": 0.1, "calmar": None}], "calmar")["trial"], 7)
        self.assertEqual(pareto.select(self.ROWS[:2], "knee")["trial"], 1)
        with self.assertRaises(ValueError):
            pareto.select([], "calmar")
        with self.assertRaises(ValueError):
            pareto.select(self.ROWS, "median")


class Case(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg0, cls.risk, cls.analyst, cls.sp = load_space()

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.cfg = make_settings(str(Path(tmp.name) / "data"))

    def run_study(self, **spec):
        st = Study.create(self.cfg, self.sp, {"name": "k", "active": ACTIVE, "variants": 0, "seed": 1, **spec})
        st.run(FakeRunner, workers=1, out=io.StringIO())
        return st, read_records(st.trials_path)


class KnownOptimumTests(Case):
    def test_tpe_finds_the_optimum_and_leaves_the_infeasible_region_while_sobol_does_not(self):
        _, tpe = self.run_study(name="tpe", sampler="tpe", trials=100)
        self.cfg = make_settings(str(Path(self.cfg["paths"]["data"]).parent / "d2"))
        _, sob = self.run_study(name="sob", sampler="sobol", trials=100)
        best = lambda rs: max(r["objectives"][0] for r in rs if r["feasible"])  # noqa: E731
        self.assertGreater(best(tpe), OPTIMUM - 0.005)
        self.assertGreaterEqual(best(tpe), best(sob))
        share = lambda rs: sum(r["feasible"] for r in rs[-50:]) / 50  # noqa: E731
        self.assertGreater(share(tpe), 0.9)  # trial constraints reach the sampler (Optuna 5.0 set_constraint)
        self.assertLess(share(sob), 0.8)

    @unittest.skipUnless(importlib.util.find_spec("torch"), "the GP sampler needs PyTorch (uv sync --project hpo --extra gp)")
    def test_gp_refines_to_the_optimum_under_constraints(self):
        _, rs = self.run_study(name="gp", sampler="gp", trials=45, active=ACTIVE[:3])
        feas = [r for r in rs if r["feasible"]]
        self.assertGreater(max(r["objectives"][0] for r in feas), OPTIMUM - 0.01)
        self.assertGreater(sum(r["feasible"] for r in rs[-15:]), 12)

    def test_the_gp_sampler_names_its_missing_dependency(self):
        if importlib.util.find_spec("torch"):
            self.skipTest("PyTorch is installed")
        with self.assertRaisesRegex(ValueError, "PyTorch"):
            self.run_study(name="gpx", sampler="gp", trials=3)


class BlockTests(Case):
    def test_blocks_split_the_kept_dimensions_by_stage_and_chain(self):
        sens = {"keep": ["risk.stops.atrMultiplier", "analyst.strategies.BULL.lookback", "analyst.regime.smaFast", "risk.sizing.minNewOrderInr"],
                "dims": [], "freeze": []}
        blocks = stages.plan_blocks(self.sp, sens, {"name": "sA", "seed": 4})
        self.assertEqual([(b["stage"], b["active"]) for b in blocks], [("B1", ["analyst.strategies.BULL.lookback"]), ("B2", ["risk.stops.atrMultiplier", "risk.sizing.minNewOrderInr"]),
                                                                     ("B3", ["analyst.regime.smaFast"])])
        self.assertEqual([b.get("from") for b in blocks], [None, "sA-B1", "sA-B2"])

    def test_a_block_fixes_the_earlier_winner_and_the_fixed_values_survive_resume(self):
        st1, recs = self.run_study(name="b1", sampler="sobol", trials=30, active=ACTIVE[:3])
        fixed = stages.chain_fixed(st1.spec, recs, "calmar")
        self.assertEqual(set(fixed), set(ACTIVE[:3]))
        st2 = Study.create(self.cfg, self.sp, {"name": "b2", "active": ACTIVE[3:5], "fixed": fixed, "trials": 6, "variants": 0, "seed": 1, "sampler": "sobol"})
        st2.run(FakeRunner, workers=1, out=io.StringIO())
        r2 = read_records(st2.trials_path)
        self.assertTrue(all(all(r["values"][n] == fixed[n] for n in fixed) for r in r2))
        self.assertEqual(r2[0]["values"]["risk.sizing.nameCapPct.MidCap"], self.sp.defaults["risk.sizing.nameCapPct.MidCap"])
        st2.run(FakeRunner, workers=1, out=io.StringIO(), trials=8)  # resume re-derives every point through the fixed values
        self.assertEqual(len(read_records(st2.trials_path)), 8)

    def test_fixed_may_not_name_an_active_or_unknown_dimension(self):
        for fixed in ({ACTIVE[0]: 3.0}, {"no.such": 1}):
            with self.assertRaises(Failed):
                Study.create(self.cfg, self.sp, {"name": "bad", "active": ACTIVE[:2], "fixed": fixed})

    def test_a_study_without_a_feasible_trial_cannot_seed_the_next_block(self):
        with self.assertRaises(Failed):
            stages.chain_fixed({"name": "x", "active": [], "fixed": {}}, [{"feasible": False, "objectives": [0, 1]}], "calmar")


class AdviceTests(Case):
    def test_too_few_trials_or_no_sensitivity_stays_on_tpe(self):
        st, recs = self.run_study(name="a", sampler="tpe", trials=20)
        r = stages.recommend(recs, st.spec, self.sp, None, self.cfg)
        self.assertEqual(r["action"], "stay on tpe")
        self.assertTrue(any("sensitivity" in x for x in r["reasons"]))

    def test_a_flat_hypervolume_with_concentrated_importance_switches_to_gp(self):
        recs = [{"feasible": True, "objectives": [0.10, 0.20]}] + [{"feasible": True, "objectives": [0.05, 0.25]} for _ in range(200)]
        spec = {"active": ACTIVE, "name": "s", "seed": 1}
        sens = {"keep": ACTIVE[:3], "dims": [{"name": n, "decision": "keep"} for n in ACTIVE[:3]]}
        r = stages.recommend(recs, spec, self.sp, sens, self.cfg)
        self.assertEqual((r["action"], r["reasons"]), ("switch to gp", []))
        self.assertAlmostEqual(r["hvGain"], 0.0)
        plan = stages.plan_refine(self.sp, sens, spec, self.cfg)
        self.assertEqual((plan["sampler"], plan["trials"], plan["from"]), ("gp", 150, "s"))
        growing = recs[:100] + [{"feasible": True, "objectives": [0.30, 0.10]}] + recs[101:]
        self.assertEqual(stages.recommend(growing, spec, self.sp, sens, self.cfg)["action"], "stay on tpe")

    def test_too_many_categoricals_keep_tpe(self):
        bools = [n for n, d in self.sp.dims.items() if d.kind == "bool"][:1]
        recs = [{"feasible": True, "objectives": [0.1, 0.2]}] * 200
        r = stages.recommend(recs, {"active": bools}, self.sp, {"keep": bools}, self.cfg)
        self.assertEqual(r["action"], "stay on tpe")
        self.assertIn("categorical", r["reasons"][0])


if __name__ == "__main__":
    unittest.main()
