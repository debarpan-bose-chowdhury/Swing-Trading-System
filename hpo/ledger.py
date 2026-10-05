"""The research ledger: cumulative, never decreases. It counts how many effectively different configurations have ever been tried, which is
what the deflated Sharpe and the effective-N cap are about (raw trials of one study are mostly near-duplicates of each other).

effective N of a study = the number of clusters of its trials' post-tax daily return series (distance sqrt((1 - rho) / 2), average linkage,
cut where clusters are no more correlated than `clusterRhoMax`). Studies add up (an upper bound: two studies can contain the same
configuration), which is the conservative direction.
"""

import math
import time
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.cluster.hierarchy import fcluster, linkage
from scipy.spatial.distance import squareform

from hpo.errors import Refusal
from hpo.status import read_json, write_json


def effective_n(returns: pd.DataFrame, rho_max: float) -> int:
    """Clusters among the columns of a (days x trials) return matrix."""
    n = returns.shape[1]
    if n <= 1:
        return n
    x = returns.to_numpy(float)
    x = x - x.mean(0)
    norm = np.sqrt((x ** 2).sum(0))
    flat = norm < 1e-14  # a series without variation (never traded) is identical to every other one like it and uncorrelated with the rest
    with np.errstate(invalid="ignore", divide="ignore"):
        rho = (x.T @ x) / np.outer(norm, norm)
    rho = np.where(np.isfinite(rho), rho, 0.0)
    rho[np.ix_(flat, flat)] = 1.0
    np.fill_diagonal(rho, 1.0)
    dist = np.sqrt(np.clip((1.0 - np.clip(rho, -1, 1)) / 2.0, 0, None))
    dist = (dist + dist.T) / 2
    np.fill_diagonal(dist, 0.0)
    z = linkage(squareform(dist, checks=False), method="average")
    return int(len(set(fcluster(z, t=math.sqrt((1.0 - rho_max) / 2.0), criterion="distance"))))


class Ledger:
    def __init__(self, path: Path, cap: int, default_ratio: float):
        self.path, self.cap, self.default_ratio = path, cap, default_ratio
        self.doc = read_json(path) or {"studies": {}, "overrides": []}

    def raw_total(self) -> int:
        return sum(s["raw"] for s in self.doc["studies"].values())

    def effective_total(self, exclude: str | None = None) -> int:
        return sum(s["effective"] for n, s in self.doc["studies"].items() if n != exclude)

    def ratio(self) -> float:
        raw = self.raw_total()
        return max(self.effective_total() / raw, 0.001) if raw else self.default_ratio

    def plan(self, name: str, planned_raw: int) -> dict:
        """The cap check of a study about to run: cumulative effective N (the study's own earlier part counted by its recorded value) plus the
        effective N its planned new trials are expected to add (planned x the observed effective/raw ratio of earlier studies)."""
        own = self.doc["studies"].get(name, {"raw": 0, "effective": 0})
        expected = math.ceil(max(0, planned_raw - own["raw"]) * self.ratio())
        total = self.effective_total() + expected
        return {"cumulative": self.effective_total(), "expectedNew": expected, "total": total, "cap": self.cap, "ratio": self.ratio(), "ok": total <= self.cap}

    def check(self, name: str, planned_raw: int, override: str | None = None) -> dict:
        p = self.plan(name, planned_raw)
        if not p["ok"]:
            if not override:
                raise Refusal(f"effective-N cap: {p['cumulative']} used + about {p['expectedNew']} expected from {planned_raw} planned trials exceeds {p['cap']} "
                              f"(ratio {p['ratio']:.2f}); rerun with --override-cap \"reason\" to proceed (it is logged in the ledger and the dossier)")
            self.doc["overrides"].append({"study": name, "reason": override, "plan": p, "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
            self.save()
        return p

    def record(self, name: str, stage: str, raw: int, effective: int) -> None:
        """Upsert a study's counts; neither number ever goes down."""
        old = self.doc["studies"].get(name, {"raw": 0, "effective": 0})
        self.doc["studies"][name] = {"stage": stage, "raw": max(raw, old["raw"]), "effective": max(effective, old["effective"]), "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        self.save()

    def save(self) -> None:
        write_json(self.path, self.doc)
