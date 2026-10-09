"""The 13-week shadow period: does the shadow portfolio behave as the backtest says it should?

The limits are fixed in the dossier beforehand: the cumulative tracking gap between the shadow portfolio's realised result and the candidate's
backtest replayed over the same days must stay within +-trackingGapPp, and the shadow drawdown must not pass the backtest's 95th-percentile depth.
This tests fidelity, not edge. Both series are pre-tax TWR indices (the shadow portfolio is not taxed).
"""

import pandas as pd


def tracking(realised: pd.Series, replay: pd.Series) -> pd.DataFrame:
    """Weekly table: cumulative realised and replayed return since the first common day, the gap in pp, and the realised drawdown depth."""
    j = pd.concat([realised.rename("realised"), replay.rename("replay")], axis=1).dropna()
    if len(j) < 2:
        return pd.DataFrame(columns=["date", "realised", "replay", "gapPp", "depth"])
    cum = j / j.iloc[0] - 1.0
    cum["gapPp"] = 100.0 * (cum.realised - cum.replay)
    cum["depth"] = -(j.realised / j.realised.cummax() - 1.0)
    weekly = cum.groupby(pd.to_datetime(cum.index).to_period("W")).tail(1)
    return weekly.rename_axis("date").reset_index()


def verdict(table: pd.DataFrame, band_pp: float, p95_depth: float, weeks: int) -> dict:
    """ok / rollback against the limits fixed in the dossier; `weeks` says how far through the period the data goes."""
    if table.empty:
        return {"status": "no data", "weeks": 0}
    gap, depth = float(table.gapPp.iloc[-1]), float(table.depth.max())
    worst = float(table.gapPp.abs().max())
    reasons = []
    if worst > band_pp:
        reasons.append(f"tracking gap reached {worst:.2f} pp (band +-{band_pp} pp)")
    if depth > p95_depth:
        reasons.append(f"shadow drawdown {depth:.1%} passed the backtest's 95th-percentile depth {p95_depth:.1%}")
    return {"status": "rollback" if reasons else ("complete" if len(table) >= weeks else "ok"), "weeks": len(table), "of": weeks, "gapPp": gap, "worstGapPp": worst, "depth": depth,
            "p95Depth": p95_depth, "reasons": reasons}
