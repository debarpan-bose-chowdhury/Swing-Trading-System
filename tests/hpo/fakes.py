"""Test doubles: a settings loader that points hpo at a temp folder, and a deterministic fake runner (no data, no engine)."""

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from hpo import objective, settings as hpo_settings

REPO = Path(__file__).resolve().parents[2]
DAYS = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2012-01-02", periods=900)]
FOLDS = [(DAYS[200], DAYS[399]), (DAYS[400], DAYS[599]), (DAYS[600], DAYS[799])]


def make_settings(data_dir: str, **changes) -> dict:
    cfg = json.loads((REPO / "hpo/config/hpo.json").read_text(encoding="utf-8"))
    cfg["paths"].update({"data": data_dir, "register": str(REPO / "doc/parameter_register.csv"), "schema": str(REPO / "hpo/schema/parameters.schema.json"),
                         "extraBounds": str(REPO / "hpo/config/space_extra.json")})
    for k, v in changes.items():
        section, _, key = k.partition("__")
        cfg[section][key] = v
    hpo_settings.validate(cfg)
    return cfg


def landscape(values: dict) -> float:
    """A smooth bowl over three dimensions, with the optimum at a known place (CAGR in [-0.05, 0.25])."""
    a = (values["risk.stops.atrMultiplier"] - 3.0) / 2.0
    b = (values["risk.sizing.riskPerPositionPct"] - 0.0125) / 0.02
    c = (values["risk.sizing.minNewOrderInr"] - 3000.0) / 20000.0
    return 0.25 - 0.3 * (a * a + b * b + c * c)


class FakeRunner:
    """Maps a point to an outcome with no engine: score from `landscape`, returns a seeded noise series that follows the score,
    infeasible (min_fills) above minNewOrderInr 12000, a FAIL when `failing` says so. Picklable, so it also runs in a spawn pool."""

    def __init__(self, fail_if=None, boom_on_run: int | None = None):
        self.fail_if, self.boom_on_run, self.calls = fail_if, boom_on_run, 0

    def identity(self) -> dict:
        return {"dataHash": "data0", "codeSha": "code0", "baseConfig": "base0", "span": [DAYS[0], DAYS[799]], "folds": [list(f) for f in FOLDS], "writeOff": 0.5, "schemaVersion": "1.0.0"}

    def run(self, job: dict) -> dict:
        self.calls += 1
        if self.boom_on_run is not None and self.calls == self.boom_on_run:
            raise KeyboardInterrupt  # a kill in the middle of a trial
        v = job["values"]
        if self.fail_if and self.fail_if(v):
            raise RuntimeError("scripted failure")
        f1 = self.score(v)
        stress = job.get("stress")
        if stress:
            f1 -= 0.01 if "universe" not in stress else 0.005  # every stress costs the same fixed amount, candidate and default alike
        seed = int(hashlib.sha256(json.dumps(v, sort_keys=True).encode()).hexdigest()[:8], 16)
        r = pd.Series(np.random.default_rng(seed).normal(f1 / 252, 0.01, 800), index=DAYS[:800])
        fills = 400 if v["risk.sizing.minNewOrderInr"] <= 12000 else 10
        depth = self.depth(v, f1)
        cons = {"valid": 0.0, "aborted": 0.0, "dd_cap": depth - 0.30, "min_fills": 300 - fills, "fills_per_fold_year": 15 - 40, "min_exposure": 0.4 - 0.7}
        out = {"status": "ok", "values": [f1, depth], "constraints": cons, "returns": r, "regimeShare": {"BULL": 0.5, "TREND": 0.2, "WEAK": 0.2, "BEAR": 0.1},
               "metrics": {"cagr": f1, "maxDrawdown": -depth, "ulcerIndex": depth * 10, "calmar": f1 / depth, "fills": fills, "avgExposure": 0.7, "foldCagr": [f1, f1, f1 + 0.01],
                           "foldDrawdown": [-depth] * 3, "foldFillsPerYear": [40, 40, 40]}, "attrs": {}, "seconds": 0.01}
        if job.get("detail"):
            idx = r.index
            nav = (1 + r).cumprod() * 100000
            out["detail"] = {"nav": nav, "bench": pd.Series(np.linspace(1, 2, len(idx)), index=idx), "exposure": pd.Series(0.7, index=idx), "rung": pd.Series(0.0, index=idx),
                             "regime": pd.Series(["BULL"] * 400 + ["BEAR"] * 400, index=idx),
                             "regimes": {"BULL": {"share": 0.5, "cagr": f1, "maxDrawdown": -depth}, "BEAR": {"share": 0.5, "cagr": f1 / 2, "maxDrawdown": -depth}},
                             "stressWindows": {"2008 crisis": {"days": 60, "return": -depth, "maxDrawdown": -depth}}, "vanished": {"exits": 1, "writtenOffInr": 100.0},
                             "profile": {"fillsPerYear": {"2013": 40, "2014": 50}, "turnover": {"2013": 2.0, "2014": 2.5}, "costDrag": {"2013": 0.004, "2014": 0.005}, "names": {"2013-05": 3, "2014-05": 4}},
                             "surveillanceModelled": False, "realism": {"bands": False, "volumeCap": False, "circuitLocks": False, "settlementLag": False}}
        return out

    def score(self, v: dict) -> float:
        return landscape(v)

    def depth(self, v: dict, f1: float) -> float:
        return float(0.1 + abs(f1 - 0.25))


class SpikeRunner(FakeRunner):
    """A broad plateau (atrMultiplier 3.5 and up: CAGR 15%, depth 8%) and one narrow spike (atrMultiplier 2.5: CAGR 25%, depth 12%). Calmar prefers the spike."""

    def score(self, v: dict) -> float:
        atr = v["risk.stops.atrMultiplier"]
        return 0.25 if atr <= 2.5 else (0.15 if atr >= 3.5 else 0.02)

    def depth(self, v: dict, f1: float) -> float:
        return 0.12 if f1 >= 0.25 else 0.08


def make_runner() -> FakeRunner:
    return FakeRunner()


def make_spike_runner() -> SpikeRunner:
    return SpikeRunner()
