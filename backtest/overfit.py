"""Overfitting checks: probability of backtest overfitting (CSCV), deflated Sharpe, OOS/IS retention, parameter neighbourhood.

References: Bailey, Borwein, Lopez de Prado, Zhu (2017) for PBO; Bailey and Lopez de Prado (2014) for the deflated Sharpe ratio.
The gate needs all four to hold. Numeric limits come from backtest.json "gate"; nothing here has a built-in default.
"""

import itertools
import math
from statistics import NormalDist

import numpy as np
import pandas as pd

EULER = 0.5772156649015329
NORM = NormalDist()
OBJECTIVES = {"postTaxCagr": "max", "maxDrawdown": "max", "ulcerIndex": "min"}  # drawdown is negative, so higher is better


def sharpe(r: np.ndarray) -> float:
    """Per-period Sharpe ratio (not annualised)."""
    sd = np.std(r, ddof=1)
    return float(np.mean(r) / sd) if sd > 0 else 0.0


def deflated_sharpe(returns: np.ndarray, trial_sharpes: np.ndarray, n_trials: int | None = None) -> float:
    """Probability that the strategy's true Sharpe exceeds what the best of N noise trials would show.

    returns: the chosen strategy's per-period returns. trial_sharpes: the per-period Sharpe of every distinct trial tried
    (their variance sets the benchmark SR0). n_trials is the N of the correction; it defaults to len(trial_sharpes) and should be
    the registry's count of every distinct parameter set ever tried.
    """
    n, t = n_trials or len(trial_sharpes), len(returns)
    if n < 2 or len(trial_sharpes) < 2 or t < 3:
        raise ValueError("deflated Sharpe needs at least 2 trials and 3 observations")
    s = pd.Series(returns)
    sr, skew, kurt = sharpe(returns), float(s.skew()), float(s.kurt()) + 3.0  # pandas gives excess kurtosis
    sr0 = math.sqrt(float(np.var(trial_sharpes, ddof=1))) * ((1 - EULER) * NORM.inv_cdf(1 - 1 / n) + EULER * NORM.inv_cdf(1 - 1 / (n * math.e)))
    denom = math.sqrt(max(1 - skew * sr + (kurt - 1) / 4 * sr**2, 1e-12))
    return NORM.cdf((sr - sr0) * math.sqrt(t - 1) / denom)


def pbo_cscv(returns: pd.DataFrame, blocks: int = 16) -> dict:
    """Probability of backtest overfitting by combinatorially symmetric cross-validation.

    returns: rows = periods, columns = trials (same dates for all). The rows are cut into `blocks` contiguous blocks; for every
    way to pick half of them as in-sample the best in-sample trial is ranked out of sample. PBO = share of splits where it lands
    in the lower half (logit of its relative rank <= 0).
    """
    t, n = returns.shape
    if blocks % 2 or blocks < 4 or n < 2 or t < blocks * 2:
        raise ValueError("pbo_cscv needs an even number of blocks >= 4, at least 2 trials and 2 rows per block")
    x = returns.to_numpy(float)
    edges = np.linspace(0, t, blocks + 1).astype(int)
    cnt = np.array([edges[i + 1] - edges[i] for i in range(blocks)], float)[:, None]
    s1 = np.stack([x[edges[i]:edges[i + 1]].sum(0) for i in range(blocks)])
    s2 = np.stack([(x[edges[i]:edges[i + 1]] ** 2).sum(0) for i in range(blocks)])
    combos = np.array([[1.0 if i in c else 0.0 for i in range(blocks)] for c in itertools.combinations(range(blocks), blocks // 2)])

    def sharpes(mask):
        m = mask @ cnt
        mean = mask @ s1 / m
        var = np.maximum((mask @ s2 - m * mean**2) / (m - 1), 1e-18)
        return mean / np.sqrt(var)

    ins, oos = sharpes(combos), sharpes(1.0 - combos)
    best = ins.argmax(1)
    rank = oos.argsort(1).argsort(1)[np.arange(len(combos)), best] + 1  # 1 = worst out of sample
    omega = rank / (n + 1)
    logit = np.log(omega / (1 - omega))
    return {"pbo": float((logit <= 0).mean()), "splits": len(combos), "trials": n, "blocks": blocks}


def retention(is_values: list[float], oos_values: list[float]) -> float | None:
    """Mean out-of-sample over mean in-sample across folds; None when the in-sample mean is not positive."""
    mi, mo = float(np.mean(is_values)), float(np.mean(oos_values))
    return mo / mi if mi > 0 else None


def neighbourhood(chosen: dict, neighbours: list[dict], tolerance: float, share: float) -> dict:
    """Plateau test: a neighbour passes when no objective is worse than the chosen point's by more than tolerance (relative).

    Better neighbours always pass. The test passes when at least `share` of the neighbours do.
    """
    ok = []
    for nb in neighbours:
        worse = [(chosen[k] - nb[k]) if d == "max" else (nb[k] - chosen[k]) for k, d in OBJECTIVES.items()]
        ok.append(all(w <= tolerance * max(abs(chosen[k]), 1e-9) for w, k in zip(worse, OBJECTIVES, strict=True)))
    frac = sum(ok) / len(ok) if ok else 0.0
    return {"neighbours": len(ok), "withinTolerance": sum(ok), "share": round(frac, 4), "passed": bool(ok) and frac >= share}


def pareto_front(objectives: pd.DataFrame) -> pd.Series:
    """True for rows no other row beats on every objective (columns named as in OBJECTIVES)."""
    v = objectives[list(OBJECTIVES)].to_numpy(float) * np.array([1.0 if d == "max" else -1.0 for d in OBJECTIVES.values()])
    keep = [not any((v[j] >= v[i]).all() and (v[j] > v[i]).any() for j in range(len(v)) if j != i) for i in range(len(v))]
    return pd.Series(keep, index=objectives.index)


def gate(limits: dict, pbo: dict, dsr: float, ret_cagr: float | None, ret_sharpe: float | None, nbhd: dict) -> dict:
    """Combine the four checks against backtest.json gate limits. All must hold."""
    checks = {
        "pbo": {"value": pbo["pbo"], "limit": limits["pboMax"], "passed": pbo["pbo"] <= limits["pboMax"]},
        "deflatedSharpe": {"value": dsr, "limit": limits["dsrMin"], "passed": dsr >= limits["dsrMin"]},
        "oosRetention": {"cagr": ret_cagr, "sharpe": ret_sharpe, "limit": limits["oosIsMin"],
                         "passed": ret_cagr is not None and ret_sharpe is not None and min(ret_cagr, ret_sharpe) >= limits["oosIsMin"]},
        "neighbourhood": {**nbhd, "limit": limits["neighbourhoodShare"], "tolerance": limits["neighbourhoodTolerance"]},
    }
    return {"passed": all(c["passed"] for c in checks.values()), "checks": checks}
