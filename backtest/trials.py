"""Trial registry and the evaluation session an HPO runner plugs into (the runner itself is out of scope for v1).

Every evaluation appends one line to backtest/data/trials/registry.jsonl: trial id, config hash, parameters, window, metrics,
seed, code sha and data hash. The count of distinct parameter sets is what the deflated Sharpe corrects for. Post-tax daily returns
go to trials/returns/<trial id>.parquet so PBO can rebuild the trial-by-period matrix without re-running anything.
"""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from backtest import api, overfit, world
from backtest.params import Schema, key_of
from backtest.walkforward import Windows


class Registry:
    def __init__(self, folder: Path):
        self.path, self.returns_dir = folder / "registry.jsonl", folder / "returns"

    def append(self, rec: dict, returns: pd.Series | None = None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if returns is not None:
            self.returns_dir.mkdir(parents=True, exist_ok=True)
            returns.rename("ret").rename_axis("date").reset_index().to_parquet(self.returns_dir / f"{rec['trialId']}.parquet", index=False)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, sort_keys=True) + "\n")

    def records(self) -> list[dict]:
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line] if self.path.exists() else []

    def n_trials(self) -> int:
        """Distinct parameter sets ever evaluated (the N of the deflated Sharpe)."""
        return len({r["paramsKey"] for r in self.records()})

    def returns(self, trial_id: str) -> pd.Series:
        df = pd.read_parquet(self.returns_dir / f"{trial_id}.parquet")
        return pd.Series(df.ret.to_numpy(), index=df.date.astype(str))


class Session:
    """One world, one schema, one set of windows and a registry. evaluate() is the single entry point for scoring a point."""

    def __init__(self, w: world.World, schema: Schema, windows: Windows, registry: Registry, holdout_marker: Path):
        self.w, self.schema, self.windows, self.registry, self.marker = w, schema, windows, registry, holdout_marker
        self.code_sha, self.data_hash = world.code_sha(), w.data.data_hash()

    def evaluate(self, point: dict, start: str, end: str, kind: str = "tuning", fold: int | None = None) -> dict:
        """Run the judge for a point over [start, end], log it, return objectives and the post-tax daily returns.

        kind "holdout" is allowed only through score_holdout(); every other kind must stay inside the tuning region.
        """
        if kind != "holdout":
            self.windows.check_tuning(start, end)
        risk, analyst = self.schema.apply(point)
        cfg = self.w.cfg
        ev = api.evaluate_config(self.w, risk, analyst, start, end)
        result, r, perf = ev.result, ev.returns, ev.metrics
        objectives = {"postTaxCagr": perf.get("cagr"), "maxDrawdown": perf.get("maxDrawdown"), "ulcerIndex": perf.get("ulcerIndex")}
        pkey = key_of(point)
        window = {"start": start, "end": end, "kind": kind, "fold": fold}
        tid = hashlib.sha256(f"{pkey}|{start}|{end}|{kind}".encode()).hexdigest()[:12]
        rec = {"trialId": tid, "configHash": world.config_hash(cfg, risk, analyst), "paramsKey": pkey, "params": point, "window": window,
               "metrics": {**objectives, "sharpe": perf.get("sharpe"), "fills": len(result.fills)}, "seed": cfg["compute"]["seed"],
               "codeSha": self.code_sha, "dataHash": self.data_hash, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        self.registry.append(rec, r)
        return {"trialId": tid, "paramsKey": pkey, "objectives": objectives, "sharpe": perf.get("sharpe"), "noTrades": len(result.fills) == 0, "returns": r}

    def score_holdout(self, point: dict) -> dict:
        """Score the untouched holdout, once, for one parameter set."""
        start, end = self.windows.holdout(self.marker, key_of(point))
        return self.evaluate(point, start, end, kind="holdout")

    def gate_report(self, point: dict, tried: list[dict]) -> dict:
        """The strict gate for a chosen point. tried: every parameter set evaluated (the chosen one included).

        Needs a confirmed parameter schema and gate limits. Cost: one full-region run per tried point not already logged,
        2 runs per rolling fold, and one run per neighbour of the chosen point.
        """
        if not self.schema.confirmed:
            raise PermissionError("params.json is not confirmed: review the bounds and set confirmed to true before running the gate")
        gate_cfg, wf = self.w.cfg["gate"], self.windows
        start, end = wf.start, wf.tuning_end
        full = {key_of(p): self.evaluate(p, start, end, "tuning") for p in tried}
        key, n_tried = key_of(point), self.registry.n_trials()  # counted before the neighbours add theirs
        matrix = pd.DataFrame({k: v["returns"] for k, v in full.items()}).dropna()
        trial_sharpes = np.array([overfit.sharpe(matrix[c].to_numpy()) for c in matrix])
        pbo = overfit.pbo_cscv(matrix)
        dsr = overfit.deflated_sharpe(matrix[key].to_numpy(), trial_sharpes, n_tried)
        folds = wf.rolling()
        ins = [self.evaluate(point, *f.train, "train", f.index) for f in folds]
        oos = [self.evaluate(point, *f.test, "test", f.index) for f in folds]
        ret_cagr = overfit.retention([x["objectives"]["postTaxCagr"] for x in ins], [x["objectives"]["postTaxCagr"] for x in oos])
        ret_sharpe = overfit.retention([x["sharpe"] for x in ins], [x["sharpe"] for x in oos])
        near = [self.evaluate(q, start, end, "neighbour")["objectives"] for q in self.schema.neighbours(point)]
        nbhd = overfit.neighbourhood(full[key]["objectives"], near, gate_cfg["neighbourhoodTolerance"], gate_cfg["neighbourhoodShare"])
        out = overfit.gate(gate_cfg, pbo, dsr, ret_cagr, ret_sharpe, nbhd)
        out |= {"paramsKey": key, "trialsTried": n_tried, "folds": len(folds), "window": [start, end], "purgeSessions": wf.purge}
        return out
