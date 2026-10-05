"""Multiple-testing statistics for the gate, over the matrix of every trial's post-tax daily returns.

  pbo_logits     probability of backtest overfitting by CSCV (S blocks) with the logit of the in-sample winner's out-of-sample rank, so the report can
                 draw its histogram (the number agrees with backtest's overfit.pbo_cscv, which does not return the logits)
  dsr_curve      the deflated Sharpe as the number of trials N grows (N and 2N are marked in the report)
  spa_pvalue     Hansen's superior predictive ability test of "no trial beats the live default", by stationary bootstrap
  medoids        at most k representative columns of a return matrix (the candidate always kept), so CSCV stays affordable
"""

import itertools
import math

import numpy as np
import pandas as pd
from scipy.cluster.hierarchy import fcluster, linkage
from scipy.spatial.distance import squareform

from backtest import api


def pbo_logits(returns: pd.DataFrame, blocks: int = 16) -> dict:
    """CSCV: rows = days, columns = trials. For every way to take half the blocks as in-sample, rank the best in-sample trial out of sample."""
    t, n = returns.shape
    if blocks % 2 or blocks < 4 or n < 2 or t < blocks * 2:
        raise ValueError("pbo_logits needs an even number of blocks >= 4, at least 2 trials and 2 rows per block")
    x = returns.to_numpy(float)
    edges = np.linspace(0, t, blocks + 1).astype(int)
    cnt = np.array([edges[i + 1] - edges[i] for i in range(blocks)], float)[:, None]
    s1 = np.stack([x[edges[i]:edges[i + 1]].sum(0) for i in range(blocks)])
    s2 = np.stack([(x[edges[i]:edges[i + 1]] ** 2).sum(0) for i in range(blocks)])
    combos = np.array([[1.0 if i in c else 0.0 for i in range(blocks)] for c in itertools.combinations(range(blocks), blocks // 2)])

    def sharpes(mask):
        m = mask @ cnt
        mean = mask @ s1 / m
        var = np.maximum((mask @ s2 - m * mean ** 2) / (m - 1), 1e-18)
        return mean / np.sqrt(var)

    ins, oos = sharpes(combos), sharpes(1.0 - combos)
    rank = oos.argsort(1).argsort(1)[np.arange(len(combos)), ins.argmax(1)] + 1
    omega = rank / (n + 1)
    logit = np.log(omega / (1 - omega))
    return {"pbo": float((logit <= 0).mean()), "logits": logit.tolist(), "splits": len(combos), "trials": n, "blocks": blocks}


def trial_sharpes(returns: pd.DataFrame) -> np.ndarray:
    return np.array([api.sharpe(returns[c].to_numpy()) for c in returns])


def dsr_curve(candidate: np.ndarray, sharpes: np.ndarray, n_values: list[int]) -> list[dict]:
    """Deflated Sharpe of the candidate if N configurations had been tried (the dispersion of the trials' Sharpe ratios fixed)."""
    out = []
    for n in n_values:
        n = max(int(n), 2)
        out.append({"n": n, "dsr": float(api.deflated_sharpe(candidate, sharpes, n))})
    return out


def medoids(returns: pd.DataFrame, k: int, keep: str | None = None) -> pd.DataFrame:
    """At most k columns that represent the matrix: clusters by return correlation, each represented by the member closest to the rest."""
    if returns.shape[1] <= k:
        return returns
    x = returns.to_numpy(float)
    x = x - x.mean(0)
    norm = np.sqrt((x ** 2).sum(0))
    with np.errstate(invalid="ignore", divide="ignore"):
        rho = np.nan_to_num((x.T @ x) / np.outer(norm, norm), nan=0.0)
    np.fill_diagonal(rho, 1.0)
    dist = np.sqrt(np.clip((1 - np.clip(rho, -1, 1)) / 2, 0, None))
    dist = (dist + dist.T) / 2
    np.fill_diagonal(dist, 0.0)
    labels = fcluster(linkage(squareform(dist, checks=False), method="average"), t=k, criterion="maxclust")
    cols = []
    for lab in sorted(set(labels)):
        idx = np.where(labels == lab)[0]
        cols.append(idx[np.argmax(rho[np.ix_(idx, idx)].mean(1))])
    names = [returns.columns[i] for i in cols]
    if keep is not None and keep not in names:
        names.append(keep)
    return returns[names]


def _stationary_indices(t: int, b: int, mean_block: float, rng: np.random.Generator) -> np.ndarray:
    """B x T indices of a stationary bootstrap (geometric block lengths with the given mean)."""
    p = 1.0 / mean_block
    idx = np.empty((b, t), dtype=np.int64)
    idx[:, 0] = rng.integers(0, t, b)
    restart = rng.random((b, t)) < p
    fresh = rng.integers(0, t, (b, t))
    for i in range(1, t):
        idx[:, i] = np.where(restart[:, i], fresh[:, i], (idx[:, i - 1] + 1) % t)
    return idx


def spa_pvalue(excess: pd.DataFrame, draws: int = 1000, mean_block: float = 20.0, seed: int = 1) -> dict:
    """Hansen (2005) SPA. excess: days x trials, each trial's daily return minus the benchmark's (the live default's). H0: no trial beats the benchmark.
    Studentised statistic; bootstrap null centred on zero except for clearly poor trials (the consistent recentring). Small p = some trial beats it."""
    d = excess.to_numpy(float)
    t, k = d.shape
    dbar = d.mean(0)
    rng = np.random.default_rng(seed)
    idx = _stationary_indices(t, draws, mean_block, rng)
    boot = np.stack([d[idx[b]].mean(0) for b in range(draws)])  # draws x k
    omega = np.maximum(np.sqrt(t * boot.var(0, ddof=1)), 1e-12)
    stat = max(0.0, float((np.sqrt(t) * dbar / omega).max()))
    cut = -np.sqrt(omega ** 2 / t * 2 * math.log(math.log(max(t, 3))))
    mu_c = np.where(dbar <= cut, dbar, 0.0)  # a clearly poor trial keeps its negative mean in the null; every other one is centred on zero
    null = np.sqrt(t) * (boot - dbar + mu_c) / omega
    null_stat = np.maximum(0.0, null.max(1))
    return {"p": float((null_stat >= stat).mean()), "statistic": stat, "trials": k, "days": t, "draws": draws, "bestTrial": excess.columns[int(np.argmax(dbar / omega))]}
