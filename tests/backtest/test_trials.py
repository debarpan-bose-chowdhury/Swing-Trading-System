"""Registry and Session: a real-engine evaluation, holdout protection, and the gate pipeline on scripted (fake) engines."""

import hashlib
import json
import random
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd

from app.risk import evaluator
from backtest import params, trials, walkforward, world
from tests.backtest.test_params_walkforward import BT, schema
from tests.backtest.helpers import REPO
from tests.backtest.test_phase4 import SingleRun


class EngineSession(SingleRun):
    """Real engine on the synthetic world, with windows scaled to its 900 days."""

    def setUp(self):
        super().setUp()
        self.w = world.World.build(self.bt)
        self.schema = params.Schema(dict(json.loads((REPO / "backtest/config/params.json").read_text(encoding="utf-8")), confirmed=False), self.w.risk, self.w.analyst)
        wf = dict(BT["walkforward"], trainYears=1, testYears=1, purgeDays=10)
        self.windows = walkforward.Windows(list(self.w.data.index.Date), self.days[300], wf, 1, 0)
        self.session = trials.Session(self.w, self.schema, self.windows, trials.Registry(Path("trials")), Path("trials/holdout.json"))
        self.point = self.schema.defaults()
        self.a, self.b = self.days[400], self.days[440]

    def test_evaluation_is_logged_reproducible_and_counted_once_per_parameter_set(self):
        r1 = self.session.evaluate(self.point, self.a, self.b)
        r2 = self.session.evaluate(self.point, self.a, self.b)
        pd.testing.assert_series_equal(r1["returns"], r2["returns"])
        self.assertEqual(r1["objectives"], r2["objectives"])
        recs = self.session.registry.records()
        self.assertEqual(len(recs), 2)
        self.assertEqual(self.session.registry.n_trials(), 1)
        rec = recs[0]
        self.assertEqual(set(rec) >= {"trialId", "configHash", "params", "window", "metrics", "seed", "codeSha", "dataHash"}, True)
        self.assertEqual(rec["dataHash"], self.w.data.data_hash())
        pd.testing.assert_series_equal(self.session.registry.returns(rec["trialId"]), r1["returns"], check_names=False)
        other = self.session.evaluate({**self.point, "stops.atrMultiplier": 3.0}, self.a, self.b)
        self.assertNotEqual(other["trialId"], r1["trialId"])
        self.assertEqual(self.session.registry.n_trials(), 2)

    def test_tuning_cannot_reach_the_holdout_and_an_invalid_point_never_runs(self):
        n = len(self.session.registry.records())
        with self.assertRaises(walkforward.HoldoutRead):
            self.session.evaluate(self.point, self.a, self.days[-1])
        with self.assertRaises(params.InvalidPoint):
            self.session.evaluate({**self.point, "stops.atrPeriod": 12}, self.a, self.b)
        self.assertEqual(len(self.session.registry.records()), n)

    def test_holdout_is_scored_once_for_one_set(self):
        self.windows.holdout_start  # exists
        first = self.session.score_holdout(self.point)
        again = self.session.score_holdout(self.point)
        self.assertEqual(first["objectives"], again["objectives"])
        with self.assertRaises(walkforward.HoldoutRead):
            self.session.score_holdout({**self.point, "stops.atrMultiplier": 3.0})

    def test_gate_refuses_while_the_schema_is_unconfirmed(self):
        with self.assertRaises(PermissionError):
            self.session.gate_report(self.point, [self.point])


class FakeEngine(trials.Session):
    """gate_report on scripted daily returns: every point has one underlying series, windows are slices of it."""

    def __init__(self, tmp: Path, edge_of, group_of=None, seed=0):
        dates = [d.date().isoformat() for d in pd.bdate_range("2008-12-01", "2026-10-02")]
        self.schema = schema(confirmed=True)
        self.windows = walkforward.Windows(dates, self.schema.common_start([d.date().isoformat() for d in pd.bdate_range("2007-09-17", "2026-10-02")]), BT["walkforward"], 2, 250)
        self.w = SimpleNamespace(cfg={"gate": BT["gate"]})
        self.registry, self.dates, self.edge_of, self.seed = trials.Registry(tmp), dates, edge_of, seed
        self.group_of = group_of or params.key_of  # points of one group share the market noise (neighbours behave alike)
        self.series: dict[str, pd.Series] = {}
        self.market: dict[str, np.ndarray] = {}

    def seed_of(self, *parts) -> int:
        return int(hashlib.sha256(repr((*parts, self.seed)).encode()).hexdigest()[:8], 16)  # not hash(): that changes per process

    def evaluate(self, point, start, end, kind="tuning", fold=None):
        key = params.key_of(point)
        if key not in self.series:
            g = self.group_of(point)
            if g not in self.market:
                self.market[g] = np.random.default_rng(self.seed_of(g)).normal(0, 0.01, len(self.dates))
            own = np.random.default_rng(self.seed_of(key, 'own')).normal(0, 0.001, len(self.dates))
            self.series[key] = pd.Series(self.edge_of(point) + self.market[g] + own, index=self.dates)
        r = self.series[key][start:end]
        perf = evaluator.perf(r, 0.055)
        rec = {"trialId": f"{key[:8]}{start}{end}", "paramsKey": key, "params": point, "window": {"kind": kind}}
        self.registry.append(rec)
        return {"trialId": rec["trialId"], "paramsKey": key, "sharpe": perf["sharpe"], "returns": r, "noTrades": False,
                "objectives": {"postTaxCagr": perf["cagr"], "maxDrawdown": perf["maxDrawdown"], "ulcerIndex": perf["ulcerIndex"]}}


class GatePipeline(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def tried(self, s, n=30):
        rng = random.Random(1)
        return [s.defaults()] + [s.sample(rng) for _ in range(n)]

    def test_a_robust_plateau_passes(self):
        # the chosen point and its neighbours share a real edge (a plateau); the other trials are noise
        s = schema(confirmed=True)
        chosen = s.defaults()
        near = {params.key_of(p) for p in s.neighbours(chosen)}
        in_plateau = lambda p: params.key_of(p) == params.key_of(chosen) or params.key_of(p) in near  # noqa: E731
        eng = FakeEngine(self.tmp, lambda p: 0.0015 if in_plateau(p) else 0.0, lambda p: "plateau" if in_plateau(p) else params.key_of(p))
        out = eng.gate_report(chosen, self.tried(s))
        self.assertTrue(out["passed"], json.dumps(out["checks"], indent=1, default=str))
        self.assertGreaterEqual(out["folds"], 7)

    def test_a_spike_among_noise_fails(self):
        # an edge exists only at the chosen point: a spike, not a plateau (and a best-of-many pick)
        s = schema(confirmed=True)
        chosen = s.defaults()
        near = {params.key_of(p) for p in s.neighbours(chosen)}
        eng = FakeEngine(self.tmp, lambda p: 0.0015 if params.key_of(p) == params.key_of(chosen) else 0.0,
                         lambda p: "plateau" if params.key_of(p) == params.key_of(chosen) or params.key_of(p) in near else params.key_of(p))
        out = eng.gate_report(chosen, self.tried(s))
        self.assertFalse(out["checks"]["neighbourhood"]["passed"])
        self.assertFalse(out["passed"])

    def test_best_of_many_noise_fails_the_gate(self):
        s = schema(confirmed=True)
        eng = FakeEngine(self.tmp, lambda p: 0.0)
        tried = self.tried(s, 40)
        sr = {params.key_of(p): eng.evaluate(p, eng.windows.start, eng.windows.tuning_end)["sharpe"] for p in tried}
        best = max(tried, key=lambda p: sr[params.key_of(p)])
        out = eng.gate_report(best, tried)
        self.assertFalse(out["passed"])
        self.assertFalse(out["checks"]["pbo"]["passed"] and out["checks"]["deflatedSharpe"]["passed"] and out["checks"]["neighbourhood"]["passed"])


if __name__ == "__main__":
    unittest.main()
